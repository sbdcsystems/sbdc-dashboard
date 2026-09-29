import { useState, useEffect, useMemo, useRef, memo } from 'react'
import { supabase } from './lib/supabaseClient'
import { buildSections } from './collectLogic'

// ── Additive, self-contained tab: "who should staff chase for payment today".
// Reads (once, lazily, only when opened): the full `outstanding` table.
// Everything else it needs (staff assignment, flagged, customer_type, phone/
// mobile) comes from the `customers` prop — the same customer_list_view rows
// App.jsx already has loaded, so this never re-fetches that data.
// Read-only. Never writes to Supabase. Tapping a row opens the EXISTING
// customer sheet via the onOpenCustomer prop (App's own openCustomer).
// Grouping/scoring logic lives in collectLogic.js (no JSX, plain-Node
// testable); this file is rendering + data-fetching only.

function formatINR(amount) {
  return new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 0 }).format(amount)
}
function formatCompact(amount) {
  const sign = amount < 0 ? '-' : ''
  const abs = Math.abs(amount)
  if (abs >= 1e7) return sign + '₹' + (abs / 1e7).toFixed(2) + ' Cr'
  if (abs >= 1e5) return sign + '₹' + (abs / 1e5).toFixed(1) + ' L'
  if (abs >= 1e3) return sign + '₹' + (abs / 1e3).toFixed(1) + ' K'
  return sign + '₹' + Math.round(abs)
}

function telHref(phone) {
  const digits = (phone || '').replace(/\D/g, '')
  return digits ? `tel:${digits}` : null
}
// Distinct message from the customer-sheet's generic reminder — this one is
// specific to the bill(s) actually being chased, per spec: name, amount, due
// date/days overdue, and the company name.
function collectWaHref(phone, customerName, amount, dueInfo) {
  const digits = (phone || '').replace(/\D/g, '').slice(-10)
  if (digits.length !== 10) return null
  const msg = `Hi ${customerName}, this is a reminder from Supreme Balaji Dye Chem — ${formatINR(amount)} is ${dueInfo}. Please let us know when we can expect payment. Thank you.`
  return `https://wa.me/91${digits}?text=${encodeURIComponent(msg)}`
}

const IconWa = () => <svg viewBox="0 0 24 24" fill="currentColor"><path d="M4 4h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H8l-4 4V6a2 2 0 0 1 2-2Z" opacity=".95" /></svg>
const IconCall = () => <svg viewBox="0 0 24 24" fill="currentColor"><path d="M6.6 10.8a15 15 0 0 0 6.6 6.6l2.2-2.2c.3-.3.7-.4 1-.2 1.1.4 2.3.6 3.6.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1A17 17 0 0 1 3 4c0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.3.2 2.5.6 3.6.1.3 0 .7-.2 1l-2.3 2.2Z" /></svg>
const IconChev = () => <svg className="chev" viewBox="0 0 8 14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m1 1 6 6-6 6" /></svg>

async function fetchAllOutstanding() {
  const PAGE = 1000
  let all = []
  let offset = 0
  while (true) {
    const { data, error } = await supabase
      .from('outstanding')
      .select('customer_id, invoice_ref, invoice_date, due_date, pending_amount, days_overdue, age_status')
      .range(offset, offset + PAGE - 1)
    if (error) throw error
    if (!data || data.length === 0) break
    all = all.concat(data)
    if (data.length < PAGE) break
    offset += PAGE
  }
  return all
}

function _fmtDueDate(iso) {
  return new Date(iso + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })
}

// ── Windowed row list — same IntersectionObserver "load more" pattern as
// App.jsx's CustomersList, duplicated locally rather than shared so this file
// stays additive and CustomersList stays untouched. ─────────────────────────
const PAGE_SIZE = 50

const CollectRow = memo(function CollectRow({ row, kind, onOpen }) {
  const c = row.customer
  const callHref = telHref(c.mobile || c.phone)
  let dueInfo
  if (kind === 'immediate') dueInfo = `${row.daysOverdue} day${row.daysOverdue === 1 ? '' : 's'} overdue`
  else dueInfo = `due ${_fmtDueDate(row.earliestDue)}`
  const waHref = collectWaHref(c.mobile, c.customer_name, row.amount, dueInfo)

  return (
    <div className="item collect-row">
      <button className="collect-row-main" onClick={() => onOpen(c)}>
        <span className="av" style={{ background: 'var(--indigo)' }}>{c.customer_name[0].toUpperCase()}</span>
        <span className="body">
          <div className="t1">{c.customer_name}</div>
          <div className="t2">
            {c.assigned_to_name || 'Unassigned'} · {row.billCount} bill{row.billCount === 1 ? '' : 's'} ·{' '}
            {kind === 'immediate' ? `${row.daysOverdue}d overdue` : `due ${_fmtDueDate(row.earliestDue)}`}
          </div>
        </span>
        <span className="val"><div className="v">{formatINR(row.amount)}</div></span>
        <IconChev />
      </button>
      <div className="collect-row-actions">
        {callHref && <a className="btn tint-blue collect-btn" href={callHref} onClick={e => e.stopPropagation()}><IconCall />Call</a>}
        {waHref && <a className="btn fill-green collect-btn" href={waHref} target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()}><IconWa />WhatsApp</a>}
      </div>
    </div>
  )
})

const CollectSection = memo(function CollectSection({ title, subtitle, rows, kind, onOpen }) {
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE)
  const sentinelRef = useRef(null)

  useEffect(() => { setVisibleCount(PAGE_SIZE) }, [rows])

  useEffect(() => {
    const el = sentinelRef.current
    if (!el) return
    const io = new IntersectionObserver(entries => {
      if (entries[0].isIntersecting) setVisibleCount(v => Math.min(v + PAGE_SIZE, rows.length))
    }, { rootMargin: '600px' })
    io.observe(el)
    return () => io.disconnect()
  }, [rows.length])

  if (rows.length === 0) return null

  const visible = rows.slice(0, visibleCount)
  const total = rows.reduce((s, r) => s + r.amount, 0)

  return (
    <section className="collect-section">
      <div className="sh">
        <h2>{title} <span className="collect-count">({rows.length})</span></h2>
      </div>
      <p className="sf" style={{ padding: '0 2px 8px' }}>{subtitle} · <strong>{formatCompact(total)}</strong> total</p>
      <div className="list">
        {visible.map(row => <CollectRow key={row.customer.id} row={row} kind={kind} onOpen={onOpen} />)}
        {visibleCount < rows.length && <div ref={sentinelRef} aria-hidden="true" />}
      </div>
    </section>
  )
})

export default function CollectTab({ customers, onOpenCustomer, lastCollectionDate, active, hidden }) {
  const [rawRows, setRawRows] = useState(null) // null = not fetched yet
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [staffFilter, setStaffFilter] = useState(null)
  const fetchedRef = useRef(false)

  // Lazy fetch — only the first time the tab is actually opened.
  useEffect(() => {
    if (!active || fetchedRef.current) return
    fetchedRef.current = true
    setLoading(true)
    fetchAllOutstanding()
      .then(rows => { setRawRows(rows); setError(null) })
      .catch(err => setError(err.message || 'Failed to load'))
      .finally(() => setLoading(false))
  }, [active])

  const customersById = useMemo(() => new Map(customers.map(c => [c.id, c])), [customers])

  const staffNames = useMemo(() => {
    const names = new Set()
    let hasUnassigned = false
    for (const c of customers) {
      if (c.assigned_to_name) names.add(c.assigned_to_name)
      else hasUnassigned = true
    }
    const sorted = [...names].sort()
    if (hasUnassigned) sorted.push('Unassigned')
    return sorted
  }, [customers])

  const sections = useMemo(() => {
    if (!rawRows) return { immediate: [], dueThisWeek: [], comingUp: [] }
    return buildSections(rawRows, customersById)
  }, [rawRows, customersById])

  const filterRow = row => {
    if (!staffFilter) return true
    if (staffFilter === 'Unassigned') return !row.customer.assigned_to_name
    return row.customer.assigned_to_name === staffFilter
  }
  const immediate   = useMemo(() => sections.immediate.filter(filterRow),   [sections, staffFilter])
  const dueThisWeek = useMemo(() => sections.dueThisWeek.filter(filterRow), [sections, staffFilter])
  const comingUp    = useMemo(() => sections.comingUp.filter(filterRow),    [sections, staffFilter])

  return (
    <main className="page" hidden={hidden}>
      <div className="lt">
        <p>Ranked by who to chase first</p>
        <h1>Collect</h1>
      </div>

      {lastCollectionDate && (
        <p className="sf" style={{ padding: '0 2px 10px' }}>
          Payments entered in Tally up to {_fmtDueDate(lastCollectionDate)}
        </p>
      )}

      {staffNames.length > 0 && (
        <div className="filters">
          <button aria-pressed={!staffFilter} onClick={() => setStaffFilter(null)}>Everyone</button>
          {staffNames.map(s => (
            <button key={s} aria-pressed={staffFilter === s} onClick={() => setStaffFilter(s)}>{s}</button>
          ))}
        </div>
      )}

      {loading && <div className="empty">Loading…</div>}
      {error && <div className="empty">Couldn't load: {error}</div>}

      {!loading && !error && rawRows && (
        <>
          {immediate.length === 0 && dueThisWeek.length === 0 && comingUp.length === 0 && (
            <div className="empty">Nothing to collect{staffFilter ? ` for ${staffFilter}` : ''} right now.</div>
          )}
          <CollectSection title="Immediate attention" subtitle="Already past due" rows={immediate} kind="immediate" onOpen={onOpenCustomer} />
          <CollectSection title="Due this week" subtitle="Due in the next 7 days" rows={dueThisWeek} kind="upcoming" onOpen={onOpenCustomer} />
          <CollectSection title="Coming up" subtitle="Due in 8–30 days" rows={comingUp} kind="upcoming" onOpen={onOpenCustomer} />
        </>
      )}
    </main>
  )
}
