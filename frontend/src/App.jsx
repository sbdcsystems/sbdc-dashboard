import { useEffect, useState, useRef, useMemo, useCallback, memo } from 'react'
import {
  BarChart, Bar, XAxis, YAxis, Tooltip,
  ResponsiveContainer, CartesianGrid,
} from 'recharts'
import { supabase } from './lib/supabaseClient'
import './App.css'

// Bucket config
const BUCKET_ORDER  = ['0-30', '30-60', '60-90', '90-120', '120+']
const BUCKET_LABELS = { '0-30': 'Under 30 days', '30-60': '30 to 60 days', '60-90': '60 to 90 days', '90-120': '90 to 120 days', '120+': 'Over 120 days' }
// Colour escalates from teal (recent/ok) → red (severely overdue). CSS vars so it
// stays correct in dark mode too (was hardcoded hex before the glass rebuild).
const BUCKET_COLORS = ['var(--green)', 'var(--yellow)', 'var(--orange)', 'var(--pink)', 'var(--red)']

// Hardcoded per known staff — falls back to neutral for Unassigned / any future name
const STAFF_COLORS = {
  'Venkatesh':    { bg: '#0A84FF', fg: '#fff' },
  'Thiagarajan':  { bg: '#FF9F0A', fg: '#fff' },
  'Gowtham':      { bg: '#30B0C7', fg: '#fff' },
  'Vijaya Priya': { bg: '#FF2D55', fg: '#fff' },
}
const NEUTRAL_COLOR  = { bg: '#8E8E93', fg: '#fff' }
const _STAFF_NAMES   = Object.keys(STAFF_COLORS)

// Always use IST (UTC+5:30) for date comparisons — DB rows are stored with IST dates
const _istNow  = () => new Date(Date.now() + 330 * 60 * 1000)
const _fmtDate = d =>
  `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')}`

const TODAY = _fmtDate(_istNow())

const MONTH_START = (() => {
  const d = _istNow(); d.setUTCDate(1); return _fmtDate(d)
})()

const LAST_MONTH_START = (() => {
  const d = _istNow(); d.setUTCDate(1); d.setUTCMonth(d.getUTCMonth() - 1); return _fmtDate(d)
})()
const LAST_MONTH_END = (() => {
  const d = _istNow(); d.setUTCDate(0); return _fmtDate(d)
})()
const FY_START = (() => {
  const d = _istNow()
  return _fmtDate(new Date(Date.UTC(d.getUTCMonth() >= 3 ? d.getUTCFullYear() : d.getUTCFullYear() - 1, 3, 1)))
})()

const FY_MONTHS_ELAPSED = (() => {
  const s = new Date(FY_START + 'T00:00:00Z')
  const t = _istNow()
  return Math.max(1, (t.getUTCFullYear() - s.getUTCFullYear()) * 12 + t.getUTCMonth() - s.getUTCMonth() + 1)
})()

// ── Pure-UTC date walking ──────────────────────────────────────────────────
// FIX (one-day chart shift): the previous version walked days with
// `new Date(dateStr + 'T00:00:00')` (parsed as browser LOCAL time) and then
// read it back with `_fmtDate`'s UTC getters. For any IST viewer that pair
// is inconsistent: local midnight IST is 18:30 the PREVIOUS day in UTC, so
// the UTC getters silently reported yesterday's date. Every date key in the
// sales chart was off by one day, and TODAY's key never actually got built
// (it was one day past the end of the loop's real range), so today's bar was
// always missing. These helpers never construct a Date from a local-time
// string — they do the arithmetic on the Y/M/D integers directly and only
// touch Date.UTC for the final read, so there's no local-timezone step to
// go wrong regardless of what timezone the viewer's browser is in.
function _addDaysISO(dateStr, n) {
  const [y, m, d] = dateStr.split('-').map(Number)
  const dt = new Date(Date.UTC(y, m - 1, d))
  dt.setUTCDate(dt.getUTCDate() + n)
  return _fmtDate(dt)
}
function _fyMonthKeys(fyStartStr, todayStr) {
  let [y, m] = fyStartStr.split('-').map(Number)
  const [ty, tm] = todayStr.split('-').map(Number)
  const months = []
  while (y < ty || (y === ty && m <= tm)) {
    months.push({
      key:   `${y}-${String(m).padStart(2, '0')}`,
      label: new Date(Date.UTC(y, m - 1, 1)).toLocaleString('en-IN', { month: 'short', timeZone: 'UTC' }),
    })
    m += 1
    if (m > 12) { m = 1; y += 1 }
  }
  return months
}
// Working days (Sun = weekly off — not documented anywhere in the codebase,
// this is the one assumption in this file that's a guess, not a fact) that
// have elapsed strictly after fromDateStr, up to and including toDateStr.
function _workingDaysSince(fromDateStr, toDateStr) {
  let count = 0
  let cur = fromDateStr
  while (cur < toDateStr) {
    cur = _addDaysISO(cur, 1)
    const [y, m, d] = cur.split('-').map(Number)
    if (new Date(Date.UTC(y, m - 1, d)).getUTCDay() !== 0) count++
  }
  return count
}
function _isOfficeHoursIST() {
  const d = _istNow()
  const mins = d.getUTCHours() * 60 + d.getUTCMinutes()
  return mins >= 10 * 60 && mins <= 18 * 60 + 30
}

// ── Card date-nav helpers ──────────────────────────────────────────────────

function _cardRange(period) {
  if (period === 'yesterday') {
    const y = _addDaysISO(TODAY, -1)
    return { from: y, to: y }
  }
  if (period === 'this_week') {
    const d = _istNow()
    const dow = d.getUTCDay()
    d.setUTCDate(d.getUTCDate() - (dow === 0 ? 6 : dow - 1))
    return { from: _fmtDate(d), to: TODAY }
  }
  if (period === 'this_month') return { from: MONTH_START, to: TODAY }
  return { from: TODAY, to: TODAY }
}

function _mergeCardRows(rows) {
  if (!rows || rows.length === 0) return null
  return {
    total_amount:  rows.reduce((s, r) => s + (Number(r.total_amount) || 0), 0),
    invoice_count: rows.reduce((s, r) => s + (r.invoice_count || 0), 0),
    synced_at:     rows[rows.length - 1]?.synced_at ?? null,
    items:         rows.flatMap(r => Array.isArray(r.items) ? r.items : []),
  }
}

function _periodLabel(period, customDate) {
  if (period === 'custom' && customDate)
    return new Date(customDate + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' })
  return { today: 'today', yesterday: 'yesterday', this_week: 'this week', this_month: 'this month' }[period] || 'selected period'
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function getInitials(name) {
  if (!name) return '?'
  const parts = name.trim().split(/\s+/)
  return parts.length >= 2
    ? (parts[0][0] + parts[1][0]).toUpperCase()
    : name[0].toUpperCase()
}

function formatINR(amount) {
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: 'INR',
    maximumFractionDigits: 0,
  }).format(amount)
}

// FIX (negative amounts): every branch below required `amount >= threshold`,
// so any negative number (an on-account credit exceeding dues — e.g. the
// Unassigned group after netting) fell through every check and hit the final
// `return '₹' + Math.round(amount)`, printing "₹-593347" with no thousands
// separator or unit. Now formats the magnitude the same way regardless of
// sign and puts the minus sign outside the ₹, e.g. "-₹5.93 L".
function formatCompact(amount) {
  const sign = amount < 0 ? '-' : ''
  const abs  = Math.abs(amount)
  if (abs >= 1e7) return sign + '₹' + (abs / 1e7).toFixed(2) + ' Cr'
  if (abs >= 1e5) return sign + '₹' + (abs / 1e5).toFixed(1) + ' L'
  if (abs >= 1e3) return sign + '₹' + (abs / 1e3).toFixed(1) + ' K'
  return sign + '₹' + Math.round(abs)
}

// ── Count-up animation ───────────────────────────────────────────────────────

function useCountUp(target, durationMs) {
  const [value, setValue] = useState(0)
  const startRef = useRef(null)

  useEffect(() => {
    if (!target) { setValue(0); return }
    const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (prefersReduced) { setValue(target); return }
    let frame
    startRef.current = null
    function step(timestamp) {
      if (!startRef.current) startRef.current = timestamp
      const progress = Math.min((timestamp - startRef.current) / durationMs, 1)
      const eased = 1 - Math.pow(1 - progress, 3)
      setValue(Math.round(target * eased))
      if (progress < 1) frame = requestAnimationFrame(step)
    }
    frame = requestAnimationFrame(step)
    return () => cancelAnimationFrame(frame)
  }, [target, durationMs])

  return value
}

function HeroFigure({ amount }) {
  const animated = useCountUp(amount, 900)
  return <span className="big xl">{formatINR(animated)}</span>
}

// ── Customer history fetch — ilike + fuzzy fallback ─────────────────────────
// sales_history stores raw Tally PARTYLEDGERNAME which may differ in case
// from customers.customer_name. Try case-insensitive exact match first;
// if 0 rows, strip common business suffixes and do a prefix LIKE search.

function _dedupHistory(rows) {
  const seen = new Set()
  return rows
    .filter(r => { if (seen.has(r.voucher_number)) return false; seen.add(r.voucher_number); return true })
    .sort((a, b) => (b.sale_date || '').localeCompare(a.sale_date || ''))
}

async function fetchCustHistory(custName, custId) {
  const base = () =>
    supabase.from('sales_history')
      .select('sale_date, voucher_number, amount')
      .gte('sale_date', FY_START)
      .order('sale_date', { ascending: false })
      .limit(200)

  // Strip common business suffixes → broader prefix search
  const stem = custName
    .replace(/[\s,.]*\b(pvt\.?\s*ltd\.?|private\s+limited|limited|ltd\.?|&\s*co\.?|and\s+co\.?|company|traders?|trading|mills?|industries|enterprises?|exports?|works?|dyers?|textiles?|fabrics?|dyeing|chemicals?|colours?|colors?)\s*\.?\s*$/i, '')
    .trim()

  // Strip honorific/business prefixes from stem → contained-phrase search
  // e.g. "Sree Laksme Narayan Fabrics" → stem "Sree Laksme Narayan" → core "Laksme Narayan"
  const core = stem
    .replace(/^(?:sree|sri|shri|m\s*[\/\.]\s*s\.?|the)\s+/i, '')
    .trim()

  const stemDiffers = stem.length >= 3 && stem.toLowerCase() !== custName.toLowerCase()
  const coreDiffers = core.length >= 4 && core.toLowerCase() !== stem.toLowerCase() && core.split(/\s+/).length >= 2

  // Build all searches up front and fire in parallel — no early return that could
  // skip fallback name searches when UUID rows already exist but are incomplete
  // (e.g. June has UUID rows, but April/May records have customer_id NULL + name drift).
  const queries = [
    custId ? base().eq('customer_id', custId) : Promise.resolve({ data: [] }),  // UUID-stamped rows
    base().is('customer_id', null).ilike('customer_name', custName),              // exact name, null-UUID
    ...(stemDiffers ? [base().is('customer_id', null).ilike('customer_name', `${stem}%`)] : []),
    ...(coreDiffers ? [base().is('customer_id', null).ilike('customer_name', `%${core}%`)] : []),
  ]

  const results = await Promise.all(queries)
  return _dedupHistory(results.flatMap(r => r.data || []))
}

// ── Rating algorithm ─────────────────────────────────────────────────────────

function computeRating(bills, history, fyMedian, monthsElapsed) {
  const posBills = bills.filter(b => b.pending_amount > 0)
  let payScore = 0
  if (posBills.length > 0) {
    const avgDue = posBills.reduce((s, b) => s + (b.days_overdue || 0), 0) / posBills.length
    // days_overdue is already relative to each customer's specific due date
    // negative = paid early, 0-15 = on time, 15-30 = slightly late, 30-60 = late, 60+ = very late
    payScore = avgDue < 0 ? 2.0 : avgDue <= 15 ? 1.5 : avgDue <= 30 ? 1.0 : avgDue <= 60 ? 0.5 : 0
  }
  const ipm = history.length / Math.max(1, monthsElapsed)
  const freqScore = ipm >= 4 ? 1.5 : ipm >= 2 ? 1.0 : ipm >= 0.8 ? 0.5 : 0.25
  const fyTotal = history.reduce((s, h) => s + h.amount, 0)
  const volScore = fyMedian > 0
    ? (fyTotal >= 2 * fyMedian ? 1.5 : fyTotal >= fyMedian ? 1.0 : fyTotal >= 0.5 * fyMedian ? 0.5 : 0)
    : (fyTotal > 0 ? 0.5 : 0)
  const total = payScore + freqScore + volScore
  const stars = Math.max(1, Math.min(5, Math.round(total)))
  let reason
  if (history.length === 0) reason = 'No purchase history this FY'
  else if (stars >= 4) reason = 'Consistent buyer, pays on time'
  else if (payScore <= 0.5) reason = 'Overdue payments dragging score'
  else if (freqScore <= 0.25) reason = 'Infrequent purchases'
  else if (volScore === 0) reason = 'Low purchase volume vs average'
  else reason = 'Average account'
  return { stars, reason }
}

// ── Contact links (Call / WhatsApp reminder) ────────────────────────────────

function telHref(phone) {
  const digits = (phone || '').replace(/\D/g, '')
  return digits ? `tel:${digits}` : null
}
function waHref(phone, amount) {
  const digits = (phone || '').replace(/\D/g, '').slice(-10)
  if (digits.length !== 10) return null
  const amountPart = amount > 0 ? ` of ${formatINR(amount)}` : ''
  const msg = `Hi, this is a reminder from Supreme Balaji Dye Chem regarding your outstanding balance${amountPart}. Please let us know when we can expect payment. Thank you.`
  return `https://wa.me/91${digits}?text=${encodeURIComponent(msg)}`
}

// ── Icons (inline, no icon library) ─────────────────────────────────────────

const Icon = {
  more:   () => <svg viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>,
  check:  () => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="m5 12 5 5 9-10"/></svg>,
  rupee:  () => <svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm1 15.9V19h-2v-1.1c-1.7-.3-3-1.4-3.1-3h2c.1.8.9 1.4 2.1 1.4 1.3 0 2-.6 2-1.3 0-.8-.6-1.1-2.3-1.5-2-.4-3.4-1.1-3.4-2.9 0-1.4 1.1-2.5 2.7-2.8V6.6h2v1.2c1.6.3 2.6 1.4 2.7 2.8h-2c-.1-.8-.8-1.2-1.8-1.2-1.1 0-1.7.5-1.7 1.2 0 .7.6 1 2.2 1.4 2.2.5 3.5 1.2 3.5 3 0 1.4-1.1 2.6-2.9 2.9Z"/></svg>,
  sold:   () => <svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 4h10l1 3h3v2h-1.2l-1.3 10.2A2 2 0 0 1 16.5 21h-9a2 2 0 0 1-2-1.8L4.2 9H3V7h3l1-3Zm1.4 3h7.2l-.3-1H8.7l-.3 1Z"/></svg>,
  received: () => <svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm0 15-5-5h3.5V7h3v5H17l-5 5Z"/></svg>,
  warn:   () => <svg viewBox="0 0 24 24" fill="currentColor"><path d="M11 6h2v8h-2zM11 16h2v2h-2z"/></svg>,
  people: () => <svg viewBox="0 0 24 24" fill="currentColor"><path d="M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm0 2c-3.9 0-7 2.2-7 5v2h14v-2c0-2.8-3.1-5-7-5Zm8-2a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Zm1 2h-.9c1.2 1.1 1.9 2.5 1.9 4v3h4v-2.5c0-2.4-2.3-4.5-5-4.5Z"/></svg>,
  chart:  () => <svg viewBox="0 0 24 24" fill="currentColor"><path d="M4 13h4v7H4zM10 8h4v12h-4zM16 4h4v16h-4z"/></svg>,
  search: () => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.6-3.6"/></svg>,
  close:  () => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>,
  call:   () => <svg viewBox="0 0 24 24" fill="currentColor"><path d="M6.6 10.8a15 15 0 0 0 6.6 6.6l2.2-2.2c.3-.3.7-.4 1-.2 1.1.4 2.3.6 3.6.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1A17 17 0 0 1 3 4c0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.3.2 2.5.6 3.6.1.3 0 .7-.2 1l-2.3 2.2Z"/></svg>,
  wa:     () => <svg viewBox="0 0 24 24" fill="currentColor"><path d="M4 4h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H8l-4 4V6a2 2 0 0 1 2-2Z" opacity=".95"/></svg>,
  chev:   () => <svg className="chev" viewBox="0 0 8 14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m1 1 6 6-6 6"/></svg>,
}

// ── Customers list — windowed + memoized ────────────────────────────────────
// Was rendering all ~1,100 rows unconditionally (just CSS `hidden` when the
// Overview tab was active), which meant React reconciled ~1,100 DOM nodes on
// every render of the whole app — including a sheet close, which has nothing
// to do with this list. React.memo means an unrelated parent re-render (e.g.
// selectedCustomer changing) skips this component entirely as long as
// `customers`/`highlightedId`/`onOpen` haven't changed; the windowing on top
// of that caps how much ever needs to mount even when it does re-render.
const CUSTOMERS_PAGE_SIZE = 50

const CustomersList = memo(function CustomersList({ customers, highlightedId, onOpen }) {
  const [visibleCount, setVisibleCount] = useState(CUSTOMERS_PAGE_SIZE)
  const sentinelRef = useRef(null)

  // Reset the window when the filtered set changes; if the row we're meant to
  // scroll to (jumped here from search) sits beyond the default window,
  // expand far enough up front to include it.
  useEffect(() => {
    if (highlightedId) {
      const idx = customers.findIndex(c => c.id === highlightedId)
      if (idx >= 0) { setVisibleCount(Math.max(CUSTOMERS_PAGE_SIZE, idx + CUSTOMERS_PAGE_SIZE)); return }
    }
    setVisibleCount(CUSTOMERS_PAGE_SIZE)
  }, [customers, highlightedId])

  useEffect(() => {
    const el = sentinelRef.current
    if (!el) return
    const io = new IntersectionObserver(entries => {
      if (entries[0].isIntersecting) {
        setVisibleCount(v => Math.min(v + CUSTOMERS_PAGE_SIZE, customers.length))
      }
    }, { rootMargin: '600px' })
    io.observe(el)
    return () => io.disconnect()
  }, [customers.length])

  if (customers.length === 0) {
    return <div className="empty">No customers match your search.</div>
  }

  const visible = customers.slice(0, visibleCount)

  return (
    <div className="list">
      {visible.map(c => (
        <button
          className={'item' + (c.id === highlightedId ? ' item--flash' : '')}
          key={c.id}
          onClick={() => onOpen(c)}
          ref={c.id === highlightedId ? el => el && el.scrollIntoView({ behavior: 'smooth', block: 'center' }) : null}
        >
          <span className="av" style={{ background: c.flagged ? 'var(--red)' : 'var(--indigo)' }}>{c.customer_name[0].toUpperCase()}</span>
          <span className="body">
            <div className="t1">{c.customer_name}</div>
            <div className="t2">
              {c.assigned_to_name || 'Unassigned'}, {c.customer_type === 'cash' ? 'cash' : `${c.credit_days || '—'} day credit`}
              {c.flagged ? ` · ${c.flagged_reason}` : ''}
            </div>
          </span>
          <span className="val"><div className="v">{c.present_pending !== 0 ? formatINR(c.present_pending) : '—'}</div></span>
          <Icon.chev />
        </button>
      ))}
      {visibleCount < customers.length && <div ref={sentinelRef} aria-hidden="true" />}
    </div>
  )
})

// ── Overview page — memoized ─────────────────────────────────────────────────
// Every prop below is either a primitive (compared by value) or already
// wrapped in useMemo/useCallback in App, so React.memo's shallow comparison
// actually holds across an unrelated re-render (e.g. opening/closing the
// customer sheet) and this whole tree — hero card, both list-heavy cards,
// two Recharts charts, staff/top-buyers/flagged lists — is skipped entirely
// rather than being re-rendered and reconciled for nothing.
const OverviewPage = memo(function OverviewPage({
  summary, presentRatio, agingTotal,
  salesCardPeriod, salesPickerOpen, salesCustomDate, salesCardLoading, salesDisplayData, salesHasDetail, visibleSalesItems, sortedSalesItems, showAllSales,
  collCardPeriod, collPickerOpen, collCustomDate, collCardLoading, collDisplayData, collectionsHasDetail, sortedCollectionItems,
  lastCollectionDate, collectionsStale,
  staffSummary, maxStaffPending,
  salesChartPeriod, hasSalesHistory, chartData, periodTotal, periodCount, chartPalette,
  topCustomers, flagged,
  staffById, staffByName, customersById, customerByName,
  onSalesPeriod, onSalesCustomDate, onToggleSalesPicker, onToggleShowAllSales,
  onCollPeriod, onCollCustomDate, onToggleCollPicker,
  onGoToStaff, onSalesChartPeriod, onOpenCustomer,
}) {
  if (!summary) return null
  return (
    <>
      <div className="lt">
        <p>{new Date(TODAY + 'T00:00:00').toLocaleDateString('en-IN', { weekday: 'long', day: 'numeric', month: 'long' })}</p>
        <h1>Summary</h1>
      </div>

      <div className="stack">
        {/* Hero — money owed, with present/archived split folded in */}
        <section className="card hero">
          <div className="mhead">
            <span className="mlabel" style={{ color: 'var(--green)' }}><Icon.rupee />Money owed to you</span>
          </div>
          <div className="mval"><HeroFigure amount={summary.recentTotal} /></div>
          <div className="exact"><b>{formatINR(summary.recentTotal)}</b> from {summary.recentCount} active customers</div>
          <div className="capsule" aria-hidden="true">
            <i style={{ width: `${presentRatio}%`, background: 'var(--green)' }}></i>
            <i style={{ width: `${100 - presentRatio}%`, background: 'var(--fill2)' }}></i>
          </div>
          <div className="legend">
            <span><i className="dot" style={{ background: 'var(--green)' }}></i>Present <b>{formatCompact(summary.recentTotal)}</b></span>
            <span><i className="dot" style={{ background: 'var(--fill2)' }}></i>Older than a year <b>{formatCompact(summary.staleTotal)}</b></span>
          </div>
        </section>

        {/* Today's Sales */}
        <section className="card">
          <div className="mhead">
            <span className="mlabel" style={{ color: 'var(--orange)' }}><Icon.sold />Sold</span>
          </div>
          <div className="seg-row">
            <div className="seg" role="group" aria-label="Sales period">
              {[['today', 'Today'], ['yesterday', 'Yesterday'], ['this_week', 'Week'], ['this_month', 'Month']].map(([p, lbl]) => (
                <button key={p} aria-pressed={salesCardPeriod === p} onClick={() => onSalesPeriod(p)}>{lbl}</button>
              ))}
            </div>
            <button className="seg-date" onClick={() => onToggleSalesPicker(v => !v)}>
              {salesCardPeriod === 'custom' && salesCustomDate
                ? new Date(salesCustomDate + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })
                : '…'}
            </button>
          </div>
          {salesPickerOpen && (
            <input type="date" className="seg-date" style={{ width: '100%', marginTop: 6 }} value={salesCustomDate} max={TODAY} onChange={e => onSalesCustomDate(e.target.value)} />
          )}

          {salesCardLoading ? (
            <div className="card-skeleton"><div className="skeleton skeleton--figure" /><div className="skeleton skeleton--line" /></div>
          ) : (
            <div key={salesCardPeriod + salesCustomDate} className="card-fade">
              {salesDisplayData ? (
                <>
                  <div className="mval" style={{ marginTop: 12 }}><span className="big">{formatINR(salesDisplayData.total_amount)}</span></div>
                  <p className="msub">
                    <strong>{salesDisplayData.invoice_count}</strong>{' '}
                    {salesDisplayData.invoice_count === 1 ? 'invoice' : 'invoices'}
                    {['today', 'yesterday', 'custom'].includes(salesCardPeriod) && salesDisplayData.synced_at && (
                      <> · synced {new Date(salesDisplayData.synced_at).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Kolkata' })}</>
                    )}
                  </p>
                  {salesHasDetail && (
                    <div className="list plain" style={{ marginTop: 12 }}>
                      {visibleSalesItems.map((item, idx) => {
                        const cid      = item.customer_id
                        const nameKey  = item.customer_name?.trim().toLowerCase()
                        const staff    = (cid != null ? staffById[cid]  : undefined) ?? staffByName[nameKey] ?? 'Unassigned'
                        const cust     = (cid != null ? customersById[cid] : null) ?? customerByName[nameKey]
                        return (
                          <div className="item" key={idx} style={{ minHeight: 44, padding: '7px 0' }}>
                            <span className="body">
                              <div className={'t1' + (cust ? ' link' : '')} onClick={cust ? () => onOpenCustomer(cust) : undefined}>{item.customer_name || '—'}</div>
                              <div className="t2">{staff}{item.invoice_ref ? ` · ${item.invoice_ref}` : ''}</div>
                            </span>
                            <span className="val"><div className="v">{formatINR(item.amount)}</div></span>
                          </div>
                        )
                      })}
                      {sortedSalesItems.length > 10 && (
                        <button className="sf link" style={{ padding: '10px 0 0' }} onClick={() => onToggleShowAllSales(v => !v)}>
                          {showAllSales ? 'Show top 10' : `Show all ${sortedSalesItems.length} invoices`}
                        </button>
                      )}
                    </div>
                  )}
                  {!salesHasDetail && <p className="msub">Per-invoice breakdown not available from this Tally export</p>}
                </>
              ) : (
                <p className="msub">
                  {salesCardPeriod === 'today' ? "No data yet — sync hasn't run today" : `No data for ${_periodLabel(salesCardPeriod, salesCustomDate)}`}
                </p>
              )}
            </div>
          )}
        </section>

        {/* Collections */}
        <section className="card">
          <div className="mhead">
            <span className="mlabel" style={{ color: 'var(--blue)' }}><Icon.received />Received</span>
          </div>
          <div className="seg-row">
            <div className="seg" role="group" aria-label="Collections period">
              {[['today', 'Today'], ['yesterday', 'Yesterday'], ['this_week', 'Week'], ['this_month', 'Month']].map(([p, lbl]) => (
                <button key={p} aria-pressed={collCardPeriod === p} onClick={() => onCollPeriod(p)}>{lbl}</button>
              ))}
            </div>
            <button className="seg-date" onClick={() => onToggleCollPicker(v => !v)}>
              {collCardPeriod === 'custom' && collCustomDate
                ? new Date(collCustomDate + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })
                : '…'}
            </button>
          </div>
          {collPickerOpen && (
            <input type="date" className="seg-date" style={{ width: '100%', marginTop: 6 }} value={collCustomDate} max={TODAY} onChange={e => onCollCustomDate(e.target.value)} />
          )}

          {collCardLoading ? (
            <div className="card-skeleton"><div className="skeleton skeleton--figure" /><div className="skeleton skeleton--line" /></div>
          ) : (
            <div key={collCardPeriod + collCustomDate} className="card-fade">
              {collDisplayData ? (
                collDisplayData.invoice_count === 0 ? (
                  <p className="msub" style={{ marginTop: 12 }}>
                    {collCardPeriod === 'today'
                      ? 'Not entered yet. Payments are usually entered a day or two later.'
                      : `No collections received ${_periodLabel(collCardPeriod, collCustomDate)}`}
                  </p>
                ) : (
                  <>
                    <div className="mval" style={{ marginTop: 12 }}><span className="big">{formatINR(collDisplayData.total_amount)}</span></div>
                    <p className="msub">
                      <strong>{collDisplayData.invoice_count}</strong>{' '}
                      {collDisplayData.invoice_count === 1 ? 'receipt' : 'receipts'}
                      {['today', 'yesterday', 'custom'].includes(collCardPeriod) && collDisplayData.synced_at && (
                        <> · synced {new Date(collDisplayData.synced_at).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Kolkata' })}</>
                      )}
                    </p>
                    {collectionsHasDetail ? (
                      <div className="list plain" style={{ marginTop: 12 }}>
                        {sortedCollectionItems.map((item, idx) => {
                          const cid     = item.customer_id
                          const nameKey = item.customer_name?.trim().toLowerCase()
                          const staff   = (cid != null ? staffById[cid] : undefined) ?? staffByName[nameKey] ?? 'Unassigned'
                          const cust    = (cid != null ? customersById[cid] : null) ?? customerByName[nameKey]
                          return (
                            <div className="item" key={idx} style={{ minHeight: 44, padding: '7px 0' }}>
                              <span className="body">
                                <div className={'t1' + (cust ? ' link' : '')} onClick={cust ? () => onOpenCustomer(cust) : undefined}>{item.customer_name || '—'}</div>
                                <div className="t2">{staff}{item.invoice_ref ? ` · ${item.invoice_ref}` : ''}</div>
                              </span>
                              <span className="val"><div className="v">{formatINR(item.amount)}</div></span>
                            </div>
                          )
                        })}
                      </div>
                    ) : (
                      <p className="msub">Per-receipt breakdown not available from this Tally export</p>
                    )}
                  </>
                )
              ) : (
                <p className="msub" style={{ marginTop: 12 }}>
                  {collCardPeriod === 'today' ? "No collections yet today — sync hasn't run" : `No data for ${_periodLabel(collCardPeriod, collCustomDate)}`}
                </p>
              )}
            </div>
          )}
        </section>

        {/* Collections status — calm by default; the office normally batch-enters
            payments 1-3 days late, so this must not read as an alarm on an
            ordinary day. Only turns orange once it's genuinely unusual. */}
        {lastCollectionDate && (
          <p className="sf" style={{ padding: '0 2px', color: collectionsStale ? 'var(--orange)' : undefined }}>
            Payments entered in Tally up to{' '}
            {new Date(lastCollectionDate + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })}
            {collectionsStale && ' — nothing new in a while, worth checking'}
          </p>
        )}

        {/* By staff */}
        <div>
          <div className="sh"><h2>By staff</h2></div>
          <div className="list">
            {staffSummary.map(s => {
              const color  = STAFF_COLORS[s.staff_name] || NEUTRAL_COLOR
              const barPct = (s.total_pending / maxStaffPending) * 100
              return (
                <button className="item" key={s.staff_name} onClick={() => onGoToStaff(s.staff_name)}>
                  <span className="av" style={{ background: color.bg, color: color.fg }}>{getInitials(s.staff_name)}</span>
                  <span className="body">
                    <div className="t1">{s.staff_name}</div>
                    <div className="t2">{s.customer_count} active customers</div>
                    <div className="staff-bar-track" style={{ height: 4, background: 'var(--fill)', borderRadius: 2, overflow: 'hidden', marginTop: 5 }}>
                      <div style={{ width: `${barPct}%`, height: '100%', background: color.bg, borderRadius: 2 }} />
                    </div>
                  </span>
                  <span className="val"><div className="v">{formatCompact(s.total_pending)}</div></span>
                  <Icon.chev />
                </button>
              )
            })}
          </div>
          <p className="sf">Tap a name to see their customers.</p>
        </div>

        {/* Sales chart */}
        <div>
          <div className="sh"><h2>Sales</h2></div>
          <section className="card">
            <div className="chead">
              <div>
                <div className="clabel">
                  Total, {salesChartPeriod === 'fy' ? 'this FY' : salesChartPeriod === 'last_month' ? 'last month' : 'this month'}
                </div>
                <div className="mval" style={{ marginTop: 4 }}><span className="big">{formatINR(periodTotal)}</span></div>
                <div className="msub">{periodCount} {periodCount === 1 ? 'invoice' : 'invoices'}</div>
              </div>
              <div className="seg small" role="group" aria-label="Chart range">
                {[['this_month', 'Month'], ['last_month', 'Last'], ['fy', 'FY']].map(([p, lbl]) => (
                  <button key={p} aria-pressed={salesChartPeriod === p} onClick={() => onSalesChartPeriod(p)}>{lbl}</button>
                ))}
              </div>
            </div>
            {hasSalesHistory ? (
              <div className="chart-wrap">
                <ResponsiveContainer width="100%" height={140}>
                  <BarChart data={chartData} margin={{ top: 4, right: 0, left: 0, bottom: 0 }}>
                    <CartesianGrid vertical={false} stroke={chartPalette.grid} />
                    <XAxis dataKey="date" tick={{ fill: chartPalette.text, fontSize: 11 }} axisLine={{ stroke: chartPalette.grid }} tickLine={false} interval={salesChartPeriod === 'fy' ? 0 : 'preserveStartEnd'} />
                    <YAxis hide />
                    <Tooltip cursor={{ fill: 'rgba(48,176,199,0.08)' }} formatter={v => [formatINR(v), 'Sales']} contentStyle={{ background: chartPalette.tooltipBg, border: `1px solid ${chartPalette.tooltipBorder}`, borderRadius: 10, fontSize: 12 }} />
                    <Bar dataKey="total" fill={chartPalette.bar} radius={[3, 3, 0, 0]} maxBarSize={salesChartPeriod === 'fy' ? 60 : 32} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            ) : (
              <p className="msub">No data yet — sync hasn't run</p>
            )}
          </section>
        </div>

        {/* Age of dues */}
        <div>
          <div className="sh"><h2>Age of dues</h2></div>
          <section className="card">
            <div className="capsule" style={{ marginTop: 0, height: 12, borderRadius: 6 }}>
              {summary.bucketChartData.map((b, i) => (
                <i key={b.bucket} style={{ width: agingTotal > 0 ? `${(b.total / agingTotal) * 100}%` : 0, background: BUCKET_COLORS[i] || BUCKET_COLORS[0] }} />
              ))}
            </div>
            <div className="list plain" style={{ margin: '10px -16px -15px' }}>
              {summary.bucketChartData.map((b, i) => (
                <div className="item" key={b.bucket}>
                  <span className="dot" style={{ background: BUCKET_COLORS[i] || BUCKET_COLORS[0] }}></span>
                  <span className="body"><div className="t1">{BUCKET_LABELS[b.bucket] || b.bucket}</div></span>
                  <span className="val">
                    <div className="v">{formatCompact(b.total)}</div>
                    <div className="s">{agingTotal > 0 ? Math.round(b.total / agingTotal * 100) : 0}%</div>
                  </span>
                </div>
              ))}
            </div>
          </section>
        </div>

        {/* Top buyers */}
        {topCustomers.length > 0 && (
          <div>
            <div className="sh"><h2>Top buyers {salesChartPeriod === 'fy' ? 'this FY' : salesChartPeriod === 'last_month' ? 'last month' : 'this month'}</h2></div>
            <div className="list">
              {topCustomers.map((c, i) => {
                const cust = customerByName[c.name.trim().toLowerCase()]
                return (
                  <div className={'item' + (cust ? ' link' : '')} key={i} onClick={cust ? () => onOpenCustomer(cust) : undefined}>
                    <span className="av" style={{ background: 'var(--teal)' }}>{i + 1}</span>
                    <span className="body"><div className="t1">{c.name}</div></span>
                    <span className="val"><div className="v">{formatCompact(c.total)}</div></span>
                    {cust && <Icon.chev />}
                  </div>
                )
              })}
            </div>
          </div>
        )}

        {/* Flagged */}
        {flagged.length > 0 && (
          <div>
            <div className="sh"><h2>Flagged</h2></div>
            <div className="list card--flagged">
              {flagged.map(c => {
                const cust = customersById[c.id]
                return (
                  <div className={'item' + (cust ? ' link' : '')} key={c.id} onClick={cust ? () => onOpenCustomer(cust) : undefined}>
                    <span className="av" style={{ background: 'var(--red)' }}>!</span>
                    <span className="body"><div className="t1">{c.customer_name}</div><div className="t2">{c.flagged_reason}</div></span>
                    <span className="val"><div className="v" style={{ color: 'var(--red)' }}>{formatINR(c.total_pending)}</div></span>
                  </div>
                )
              })}
            </div>
            <p className="sf">Flagged accounts are not included in money owed.</p>
          </div>
        )}
      </div>
    </>
  )
})

// ── App ──────────────────────────────────────────────────────────────────────

const _PW = import.meta.env.VITE_DASHBOARD_PASSWORD

export default function App() {
  const [authenticated, setAuthenticated] = useState(
    () => localStorage.getItem('sbdc_auth') === _PW
  )
  const [pwInput, setPwInput]   = useState('')
  const [pwError, setPwError]   = useState(false)

  const [view, setView] = useState('overview')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [summary, setSummary] = useState(null)
  const [flagged, setFlagged] = useState([])
  const [staffSummary, setStaffSummary] = useState([])
  const [customers, setCustomers] = useState([])
  const [searchQuery, setSearchQuery] = useState('')
  const [staffFilter, setStaffFilter] = useState(null)
  const [todaySales, setTodaySales]               = useState(null)
  const [todayCollections, setTodayCollections]   = useState(null)
  const [showAllSales, setShowAllSales] = useState(false)
  const [salesHistory, setSalesHistory] = useState([])
  const [salesChartPeriod, setSalesChartPeriod] = useState('this_month')
  const [globalSearch, setGlobalSearch]           = useState('')
  const [searchOpen, setSearchOpen]               = useState(false)
  const [highlightedId, setHighlightedId]         = useState(null)
  const [selectedCustomer, setSelectedCustomer]   = useState(null)
  const [custDetail, setCustDetail]               = useState(null)
  const [custDetailLoading, setCustDetailLoading] = useState(false)

  const [salesCardPeriod, setSalesCardPeriod] = useState('today')
  const [salesCardLoading, setSalesCardLoading] = useState(false)
  const [salesCardRows, setSalesCardRows] = useState(null)
  const [salesPickerOpen, setSalesPickerOpen] = useState(false)
  const [salesCustomDate, setSalesCustomDate] = useState('')
  const [collCardPeriod, setCollCardPeriod] = useState('today')
  const [collCardLoading, setCollCardLoading] = useState(false)
  const [collCardRows, setCollCardRows] = useState(null)
  const [collPickerOpen, setCollPickerOpen] = useState(false)
  const [collCustomDate, setCollCustomDate] = useState('')

  // Sync health + last-real-receipt date — both best-effort (non-fatal if the
  // sync_status table doesn't exist yet, or daily_collections has no rows).
  const [lastSync, setLastSync]                   = useState(null)
  const [lastCollectionDate, setLastCollectionDate] = useState(null)

  // Appearance (Automatic / Light / Dark), scroll-collapsed title, menus
  const [theme, setTheme] = useState(() => {
    try { return localStorage.getItem('sbdc-appearance') || 'auto' } catch { return 'auto' }
  })
  const [menuOpen, setMenuOpen]     = useState(false)
  const [scrolled, setScrolled]     = useState(false)
  const [systemDark, setSystemDark] = useState(
    () => window.matchMedia('(prefers-color-scheme: dark)').matches
  )
  const [now, setNow] = useState(() => Date.now())

  // Auto-clear row highlight after 2.5 s
  useEffect(() => {
    if (!highlightedId) return
    const t = setTimeout(() => setHighlightedId(null), 2500)
    return () => clearTimeout(t)
  }, [highlightedId])

  // Tick once a minute so the "last synced" staleness check stays live
  // without calling Date.now() directly during render.
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 60000)
    return () => clearInterval(t)
  }, [])

  // Appearance: apply + persist
  useEffect(() => {
    if (theme === 'auto') delete document.documentElement.dataset.theme
    else document.documentElement.dataset.theme = theme
    try { localStorage.setItem('sbdc-appearance', theme) } catch { /* ignore */ }
  }, [theme])
  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const handler = e => setSystemDark(e.matches)
    mq.addEventListener('change', handler)
    return () => mq.removeEventListener('change', handler)
  }, [])
  const resolvedDark = theme === 'dark' || (theme === 'auto' && systemDark)

  // Scroll-collapsing large title
  useEffect(() => {
    function onScroll() { setScrolled(window.scrollY > 40) }
    addEventListener('scroll', onScroll, { passive: true })
    return () => removeEventListener('scroll', onScroll)
  }, [])

  useEffect(() => {
    async function load() {
      try {
        const [
          { data: statusRows,      error: e1 },
          { data: bucketRows,      error: e2 },
          { data: flaggedRows,     error: e3 },
          { data: staffRows,       error: e4 },
          { data: salesRow,        error: e6 },
          { data: collectionsRow,  error: e7 },
          { data: syncRows },
          { data: recentCollRows },
        ] = await Promise.all([
          supabase.from('outstanding_status_summary').select('*'),
          supabase.from('outstanding_bucket_summary').select('*'),
          supabase.from('flagged_customers_summary').select('*'),
          supabase.from('outstanding_by_staff_summary').select('*'),
          supabase.from('daily_sales').select('*').eq('sale_date', TODAY).maybeSingle(),
          supabase.from('daily_collections').select('*').eq('sale_date', TODAY).maybeSingle(),
          // Best-effort: sync_status may not exist yet (see CLAUDE.md — needs its
          // CREATE TABLE + GRANT run once). No `error` destructured — a missing
          // table must not fail the whole dashboard load.
          supabase.from('sync_status').select('run_at,status').eq('status', 'success').order('run_at', { ascending: false }).limit(1),
          // Lookback window to find the last day with a real receipt, for the
          // "no payments recorded" notice.
          supabase.from('daily_collections').select('sale_date,invoice_count').gte('sale_date', _addDaysISO(TODAY, -30)).lte('sale_date', TODAY).order('sale_date', { ascending: false }),
        ])

        if (e1) throw e1
        if (e2) throw e2
        if (e3) throw e3
        if (e4) throw e4
        // e6 is non-fatal — table may not exist yet

        if (syncRows && syncRows.length) setLastSync(syncRows[0])
        if (recentCollRows) {
          const lastReal = recentCollRows.find(r => (r.invoice_count || 0) > 0)
          if (lastReal) setLastCollectionDate(lastReal.sale_date)
        }

        const recentRow = statusRows.find(r => r.age_status === 'recent') || {}
        const staleRow  = statusRows.find(r => r.age_status === 'stale')  || {}

        const bucketByKey = {}
        for (const row of bucketRows) bucketByKey[row.bucket] = row

        const bucketChartData = BUCKET_ORDER
          .filter(b => bucketByKey[b])
          .map(b => ({
            bucket: b,
            total:  Number(bucketByKey[b].total_pending) || 0,
            count:  bucketByKey[b].bill_count || 0,
          }))

        setSummary({
          recentTotal: Number(recentRow.total_pending) || 0,
          recentCount: recentRow.customer_count || 0,
          staleTotal:  Number(staleRow.total_pending) || 0,
          staleCount:  staleRow.customer_count || 0,
          bucketChartData,
        })

        setFlagged(
          flaggedRows
            .map(c => ({ ...c, total_pending: Number(c.total_pending) || 0 }))
            .filter(c => c.total_pending > 0)
            .sort((a, b) => b.total_pending - a.total_pending)
        )

        setStaffSummary(
          staffRows
            .map(s => ({ ...s, total_pending: Number(s.total_pending) || 0 }))
            .sort((a, b) => b.total_pending - a.total_pending)
        )

        if (!e6 && salesRow) {
          setTodaySales({
            total_amount:  Number(salesRow.total_amount) || 0,
            invoice_count: salesRow.invoice_count || 0,
            synced_at:     salesRow.synced_at,
            items:         Array.isArray(salesRow.items) ? salesRow.items : [],
          })
        }
        if (!e7 && collectionsRow) {
          setTodayCollections({
            total_amount:  Number(collectionsRow.total_amount) || 0,
            invoice_count: collectionsRow.invoice_count || 0,
            synced_at:     collectionsRow.synced_at,
            items:         Array.isArray(collectionsRow.items) ? collectionsRow.items : [],
          })
        }

        // Paginated customer fetch — 1067 customers, PostgREST caps at 1000/request
        {
          const PAGE_C = 1000
          let allC = []
          let cOff = 0
          while (true) {
            const { data: cPage, error: cErr } = await supabase
              .from('customer_list_view')
              .select('*')
              .range(cOff, cOff + PAGE_C - 1)
            if (cErr) throw cErr
            if (!cPage) break
            allC = allC.concat(cPage)
            if (cPage.length < PAGE_C) break
            cOff += PAGE_C
          }
          setCustomers(
            allC
              .map(c => ({
                ...c,
                present_pending:  Number(c.present_pending) || 0,
                archived_pending: Number(c.archived_pending) || 0,
              }))
              .sort((a, b) => b.present_pending - a.present_pending)
          )
        }

        // Paginated fetch — PostgREST hard-caps at 1000 rows per request
        const PAGE = 1000
        let allHistory = []
        let hOffset = 0
        while (true) {
          const { data: page, error: hErr } = await supabase
            .from('sales_history')
            .select('sale_date,customer_name,amount')
            .gte('sale_date', FY_START)
            .order('sale_date', { ascending: true })
            .range(hOffset, hOffset + PAGE - 1)
          if (hErr || !page) break
          allHistory = allHistory.concat(page)
          if (page.length < PAGE) break
          hOffset += PAGE
        }
        if (allHistory.length > 0) {
          setSalesHistory(allHistory.map(s => ({ ...s, amount: Number(s.amount) || 0 })))
        }
      } catch (e) {
        setError(e.message || 'Could not load dashboard data.')
      } finally {
        setLoading(false)
      }
    }
    load()
  }, [])

  const goToStaff = useCallback((name) => {
    setView('customers')
    setStaffFilter(name)
    setSearchQuery('')
  }, [])

  // Every one of these used to be a plain `const` recomputed on every render —
  // including a render triggered by opening/closing the customer sheet, which
  // has nothing to do with any of this data. Profiling a close under 6x CPU
  // throttle showed these O(customers)/O(salesHistory) passes (up to ~1,100
  // and several thousand rows respectively) re-running on every sheet toggle,
  // on top of the Customers list's DOM reconciliation. useMemo means they only
  // redo the work when their actual inputs change.
  const staffFilteredCustomers = useMemo(() => customers.filter(c => {
    if (!staffFilter) return true
    if (staffFilter === 'Unassigned') return !c.assigned_to_name
    return c.assigned_to_name === staffFilter
  }), [customers, staffFilter])

  const filteredCustomers = useMemo(() => staffFilteredCustomers.filter(c => {
    const q = searchQuery.trim().toLowerCase()
    if (!q) return true
    return (
      c.customer_name.toLowerCase().includes(q) ||
      (c.phone || '').includes(q)
    )
  }), [staffFilteredCustomers, searchQuery])

  const staffSubtotal = useMemo(() => staffFilter ? {
    count: staffFilteredCustomers.length,
    total: staffFilteredCustomers.reduce((sum, c) => sum + c.present_pending, 0),
  } : null, [staffFilter, staffFilteredCustomers])

  const maxStaffPending = useMemo(() => staffSummary.length
    ? Math.max(...staffSummary.map(s => s.total_pending), 1)
    : 1, [staffSummary])

  const presentRatio = useMemo(() => summary && (summary.recentTotal + summary.staleTotal) > 0
    ? (summary.recentTotal / (summary.recentTotal + summary.staleTotal)) * 100
    : 50, [summary])

  // ── Sales history aggregates ────────────────────────────────────────────────
  const periodStart = salesChartPeriod === 'last_month' ? LAST_MONTH_START
    : salesChartPeriod === 'fy' ? FY_START
    : MONTH_START
  const periodEnd = salesChartPeriod === 'last_month' ? LAST_MONTH_END : TODAY

  const periodSales = useMemo(
    () => salesHistory.filter(s => s.sale_date >= periodStart && s.sale_date <= periodEnd),
    [salesHistory, periodStart, periodEnd]
  )
  const periodTotal = useMemo(() => periodSales.reduce((sum, s) => sum + s.amount, 0), [periodSales])
  const periodCount = periodSales.length

  const chartData = useMemo(() => {
    if (salesChartPeriod === 'fy') {
      const fyMonths = _fyMonthKeys(FY_START, TODAY)
      const mmap = {}
      salesHistory.forEach(s => { const m = s.sale_date.slice(0, 7); mmap[m] = (mmap[m] || 0) + s.amount })
      return fyMonths.map(({ key, label }) => ({ date: label, total: mmap[key] || 0 }))
    }
    const days = []
    let d = periodStart
    while (d <= periodEnd) { days.push(d); d = _addDaysISO(d, 1) }
    const dmap = {}
    periodSales.forEach(s => { dmap[s.sale_date] = (dmap[s.sale_date] || 0) + s.amount })
    return days.map(d => {
      const [, mo, da] = d.split('-')
      return { date: `${Number(da)}/${Number(mo)}`, total: dmap[d] || 0 }
    })
  }, [salesChartPeriod, salesHistory, periodSales, periodStart, periodEnd])

  const topCustomers = useMemo(() => {
    const topCustomerMap = {}
    periodSales.forEach(s => {
      const k = s.customer_name || 'Unknown'
      topCustomerMap[k] = (topCustomerMap[k] || 0) + s.amount
    })
    return Object.entries(topCustomerMap)
      .sort((a, b) => b[1] - a[1]).slice(0, 5)
      .map(([name, total]) => ({ name, total }))
  }, [periodSales])

  // UUID-keyed maps (primary — no string matching, immune to name variations)
  const staffById     = useMemo(() => Object.fromEntries(customers.map(c => [c.id, c.assigned_to_name || 'Unassigned'])), [customers])
  const customersById = useMemo(() => Object.fromEntries(customers.map(c => [c.id, c])), [customers])
  // Name-keyed maps (fallback — for items synced before UUID enrichment was added)
  const staffByName    = useMemo(() => Object.fromEntries(customers.map(c => [c.customer_name.trim().toLowerCase(), c.assigned_to_name || 'Unassigned'])), [customers])
  const customerByName = useMemo(() => Object.fromEntries(customers.map(c => [c.customer_name.trim().toLowerCase(), c])), [customers])

  // ── Global search ──────────────────────────────────────────────────────────
  const searchResults = useMemo(() => {
    const q = globalSearch.trim().toLowerCase()
    if (q.length < 2) return null
    // Staff intent: query is an unambiguous prefix of exactly one staff name
    const staffHits = _STAFF_NAMES.filter(s => s.toLowerCase().startsWith(q))
    if (staffHits.length === 1) {
      const staff = staffHits[0]
      return {
        type:  'staff',
        staff,
        items: customers
          .filter(c => c.assigned_to_name === staff)
          .sort((a, b) => b.present_pending - a.present_pending)
          .slice(0, 25),
      }
    }
    // Customer name search
    return {
      type:  'customers',
      items: customers.filter(c => c.customer_name.toLowerCase().includes(q)).slice(0, 25),
    }
  }, [globalSearch, customers])

  const fyMedian = useMemo(() => {
    if (!salesHistory.length) return 0
    const totals = {}
    salesHistory.forEach(s => { totals[s.customer_name] = (totals[s.customer_name] || 0) + s.amount })
    const vals = Object.values(totals).filter(v => v > 0).sort((a, b) => a - b)
    if (!vals.length) return 0
    const mid = Math.floor(vals.length / 2)
    return vals.length % 2 === 0 ? (vals[mid - 1] + vals[mid]) / 2 : vals[mid]
  }, [salesHistory])

  const cdChartData = useMemo(() => {
    if (!custDetail) return []
    const fyMonths = _fyMonthKeys(FY_START, TODAY)
    const mmap = {}
    custDetail.history.forEach(s => { const m = s.sale_date.slice(0, 7); mmap[m] = (mmap[m] || 0) + s.amount })
    return fyMonths.map(({ key, label }) => ({ date: label, total: mmap[key] || 0 }))
  }, [custDetail])

  // Lock background scroll while the sheet is open — on mobile, a scrollable
  // page behind a fixed-position sheet can rubber-band/shift under a tap,
  // which is what made the close button miss on real phones even though it
  // measured out fine in the DOM.
  useEffect(() => {
    if (!selectedCustomer) return
    const original = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => { document.body.style.overflow = original }
  }, [selectedCustomer])

  // Swipe-down-to-close on the sheet's grab handle/header only, so it
  // doesn't fight with scrolling the sheet's own content.
  const [sheetDragY, setSheetDragY] = useState(0)
  const sheetDragStartY = useRef(null)
  const sheetDragging    = useRef(false)

  // Handlers below are wrapped in useCallback (stable references across
  // renders) and, along with every derived value above, now live entirely
  // before the auth early-return — moved down from just above the login
  // gate so useCallback/useMemo can be called unconditionally. They're
  // passed as props into the now-memoized CustomersList/OverviewPage
  // components below; without a stable reference, React.memo's prop
  // comparison would fail every render and defeat the memoization.
  const handleSalesPeriod = useCallback(async (period) => {
    setSalesCardPeriod(period)
    if (period !== 'custom') setSalesPickerOpen(false)
    if (period === 'today') { setSalesCardRows(null); return }
    const { from, to } = _cardRange(period)
    setSalesCardLoading(true)
    const { data } = await supabase.from('daily_sales').select('*').gte('sale_date', from).lte('sale_date', to)
    setSalesCardRows(data || [])
    setSalesCardLoading(false)
  }, [])

  const handleSalesCustomDate = useCallback(async (dateStr) => {
    setSalesCustomDate(dateStr)
    if (!dateStr) return
    setSalesCardPeriod('custom')
    setSalesCardLoading(true)
    const { data } = await supabase.from('daily_sales').select('*').eq('sale_date', dateStr)
    setSalesCardRows(data || [])
    setSalesCardLoading(false)
  }, [])

  const handleCollPeriod = useCallback(async (period) => {
    setCollCardPeriod(period)
    if (period !== 'custom') setCollPickerOpen(false)
    if (period === 'today') { setCollCardRows(null); return }
    const { from, to } = _cardRange(period)
    setCollCardLoading(true)
    const { data } = await supabase.from('daily_collections').select('*').gte('sale_date', from).lte('sale_date', to)
    setCollCardRows(data || [])
    setCollCardLoading(false)
  }, [])

  const handleCollCustomDate = useCallback(async (dateStr) => {
    setCollCustomDate(dateStr)
    if (!dateStr) return
    setCollCardPeriod('custom')
    setCollCardLoading(true)
    const { data } = await supabase.from('daily_collections').select('*').eq('sale_date', dateStr)
    setCollCardRows(data || [])
    setCollCardLoading(false)
  }, [])

  const handleSearchResult = useCallback((customer) => {
    setGlobalSearch('')
    setSearchOpen(false)
    setView('customers')
    setStaffFilter(null)
    setSearchQuery('')
    setHighlightedId(customer.id)
  }, [])

  // Customer detail is now a bottom sheet overlaid on whichever page is
  // showing, rather than a third routed "view" — closing it just returns to
  // whatever was already underneath, so there's no separate "back" state to
  // track any more.
  const openCustomer = useCallback(async (customer) => {
    setSelectedCustomer(customer)
    setCustDetail(null)
    setCustDetailLoading(true)
    const [{ data: bills }, history] = await Promise.all([
      supabase.from('outstanding')
        .select('invoice_date, invoice_ref, pending_amount, days_overdue, age_status, bucket')
        .eq('customer_id', customer.id)
        .order('invoice_date', { ascending: true }),
      fetchCustHistory(customer.customer_name, customer.id),
    ])
    setCustDetail({ bills: bills || [], history })
    setCustDetailLoading(false)
  }, [])

  // Closing the sheet must ONLY clear this local state — no refetch, and
  // (thanks to the memoization above + CustomersList/OverviewPage being
  // React.memo'd) no re-render of the Overview/Customers tree either.
  const closeSheet = useCallback(() => {
    setSelectedCustomer(null)
    setCustDetail(null)
  }, [])

  function handleSheetDragStart(e) {
    sheetDragStartY.current = e.touches[0].clientY
    sheetDragging.current = true
  }
  function handleSheetDragMove(e) {
    if (!sheetDragging.current || sheetDragStartY.current === null) return
    const delta = e.touches[0].clientY - sheetDragStartY.current
    if (delta > 0) setSheetDragY(delta)
  }
  function handleSheetDragEnd() {
    if (!sheetDragging.current) return
    sheetDragging.current = false
    if (sheetDragY > 80) closeSheet()
    setSheetDragY(0)
    sheetDragStartY.current = null
  }

  const salesDisplayData = useMemo(() => salesCardRows !== null ? _mergeCardRows(salesCardRows) : todaySales, [salesCardRows, todaySales])
  const collDisplayData  = useMemo(() => collCardRows  !== null ? _mergeCardRows(collCardRows)  : todayCollections, [collCardRows, todayCollections])

  const sortedSalesItems      = useMemo(() => [...(salesDisplayData?.items ?? [])].sort((a, b) => b.amount - a.amount), [salesDisplayData])
  const visibleSalesItems     = useMemo(() => showAllSales ? sortedSalesItems : sortedSalesItems.slice(0, 10), [showAllSales, sortedSalesItems])
  const salesHasDetail        = useMemo(() => sortedSalesItems.some(i => i.customer_name || i.invoice_ref), [sortedSalesItems])
  const sortedCollectionItems = useMemo(() => [...(collDisplayData?.items ?? [])].sort((a, b) => b.amount - a.amount), [collDisplayData])
  const collectionsHasDetail  = useMemo(() => sortedCollectionItems.some(i => i.customer_name || i.invoice_ref), [sortedCollectionItems])

  // ── Customer detail derived values ─────────────────────────────────────────
  const {
    cdBills, cdHistory, cdActiveBills, cdTotalPending, cdOldestBill,
    cdFyTotal, cdAvgOrder, cdDaysSince, cdRating,
  } = useMemo(() => {
    const cdBills        = custDetail?.bills   ?? []
    const cdHistory      = custDetail?.history ?? []
    const cdActiveBills  = cdBills.filter(b => b.pending_amount > 0)
    const cdTotalPending = cdActiveBills.reduce((s, b) => s + Number(b.pending_amount), 0)
    const cdOldestBill   = cdActiveBills[0] ?? null
    const cdFyTotal      = cdHistory.reduce((s, h) => s + Number(h.amount), 0)
    const cdAvgOrder     = cdHistory.length > 0 ? cdFyTotal / cdHistory.length : 0
    const cdLastPurchase = cdHistory[0]?.sale_date ?? null
    const cdDaysSince    = cdLastPurchase
      ? Math.floor((new Date(TODAY) - new Date(cdLastPurchase + 'T00:00:00')) / 86400000)
      : null
    const cdRating = custDetail ? computeRating(cdBills, cdHistory, fyMedian, FY_MONTHS_ELAPSED) : null
    return { cdBills, cdHistory, cdActiveBills, cdTotalPending, cdOldestBill, cdFyTotal, cdAvgOrder, cdDaysSince, cdRating }
  }, [custDetail, fyMedian])

  // ── Collections status line — calm by default, orange only if stale ────────
  // Was an alarm-style "No payments recorded since <date>" notice that fired
  // after just 2 working days — but the office normally batch-enters
  // payments 1-3 days late, so it was permanently, wrongly alarming on
  // completely ordinary days. Now always shows the plain fact (latest date
  // Tally has entries for) and only escalates to orange once it's genuinely
  // unusual: 4+ working days with nothing new entered.
  const workingDaysSinceLastPayment = lastCollectionDate ? _workingDaysSince(lastCollectionDate, TODAY) : null
  const collectionsStale = workingDaysSinceLastPayment !== null && workingDaysSinceLastPayment >= 4

  // ── "Last synced" header pill ───────────────────────────────────────────────
  const lastSyncStale = lastSync
    ? (now - new Date(lastSync.run_at).getTime() > 60 * 60 * 1000) && _isOfficeHoursIST()
    : false

  // ── Recharts palette (theme-aware — SVG attrs don't resolve CSS var()) ─────
  const chartPalette = useMemo(() => resolvedDark
    ? { grid: '#3A3A3C', text: 'rgba(235,235,245,.6)', tooltipBg: '#1C1C1E', tooltipBorder: '#3A3A3C', bar: '#40C8E0' }
    : { grid: '#D8DBE6', text: 'rgba(60,60,67,.6)',   tooltipBg: '#fff',    tooltipBorder: '#D8DBE6', bar: '#30B0C7' }, [resolvedDark])

  const agingTotal = useMemo(() => summary ? summary.bucketChartData.reduce((s, b) => s + b.total, 0) : 0, [summary])

  const pageTitle = view === 'customers' ? 'Customers' : 'Summary'
  const custCallHref = telHref(selectedCustomer?.phone)
  const custWaHref   = waHref(selectedCustomer?.phone, cdTotalPending)

  const applyTheme = useCallback((t) => { setTheme(t); setMenuOpen(false) }, [])

  // ── Auth gate — all hooks are above this, so early return is safe ──────────

  const handleLogin = e => {
    e.preventDefault()
    if (pwInput === _PW) {
      localStorage.setItem('sbdc_auth', _PW)
      setAuthenticated(true)
    } else {
      setPwError(true)
      setPwInput('')
    }
  }

  if (!authenticated) {
    return (
      <div className="login-screen">
        <div className="login-card">
          <div className="login-logo">SB</div>
          <p className="login-title">Supreme Balaji Dye Chem</p>
          <form onSubmit={handleLogin} className="login-form">
            <input
              type="password"
              className={`login-input${pwError ? ' login-input--error' : ''}`}
              placeholder="Enter password"
              value={pwInput}
              autoFocus
              onChange={e => { setPwInput(e.target.value); setPwError(false) }}
            />
            {pwError && <p className="login-error">Incorrect password</p>}
            <button type="submit" className="login-btn">Enter</button>
          </form>
        </div>
      </div>
    )
  }

  return (
    <div className="app">
      <div className="ambient" aria-hidden="true"><i className="p1"></i><i className="p2"></i><i className="p3"></i><i className="p4"></i></div>
      <div className="edge-top" aria-hidden="true"></div>

      <header className={'nav' + (scrolled ? ' scrolled' : '')}>
        <div className="nav-left">
          <div className="inline-title">{pageTitle}</div>
          {lastSync && (
            <div className={'sync-pill' + (lastSyncStale ? ' sync-pill--stale' : '')}>
              <i />
              Synced {new Date(lastSync.run_at).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Kolkata' })}
            </div>
          )}
        </div>
        <button className="glass circle" onClick={() => setMenuOpen(v => !v)} aria-label="Options" aria-haspopup="true" aria-expanded={menuOpen}>
          <Icon.more />
        </button>
      </header>

      <div className="menu glass" hidden={!menuOpen} role="menu">
        <div className="mh">Appearance</div>
        {[['auto', 'Automatic'], ['light', 'Light'], ['dark', 'Dark']].map(([t, label]) => (
          <button key={t} role="menuitemradio" aria-checked={theme === t} onClick={() => applyTheme(t)}>
            {label}<span className="tick"><Icon.check /></span>
          </button>
        ))}
      </div>

      {loading && <main className="page"><div className="state-msg">Loading…</div></main>}
      {error && <main className="page"><div className="state-msg state-msg--error">Error: {error}</div></main>}

      {/* ── Overview / Summary page ── */}
      <main className="page" hidden={loading || !!error || view !== 'overview' || !summary}>
        <OverviewPage
          summary={summary} presentRatio={presentRatio} agingTotal={agingTotal}
          salesCardPeriod={salesCardPeriod} salesPickerOpen={salesPickerOpen} salesCustomDate={salesCustomDate}
          salesCardLoading={salesCardLoading} salesDisplayData={salesDisplayData} salesHasDetail={salesHasDetail}
          visibleSalesItems={visibleSalesItems} sortedSalesItems={sortedSalesItems} showAllSales={showAllSales}
          collCardPeriod={collCardPeriod} collPickerOpen={collPickerOpen} collCustomDate={collCustomDate}
          collCardLoading={collCardLoading} collDisplayData={collDisplayData} collectionsHasDetail={collectionsHasDetail}
          sortedCollectionItems={sortedCollectionItems}
          lastCollectionDate={lastCollectionDate} collectionsStale={collectionsStale}
          staffSummary={staffSummary} maxStaffPending={maxStaffPending}
          salesChartPeriod={salesChartPeriod} hasSalesHistory={salesHistory.length > 0} chartData={chartData}
          periodTotal={periodTotal} periodCount={periodCount} chartPalette={chartPalette}
          topCustomers={topCustomers} flagged={flagged}
          staffById={staffById} staffByName={staffByName} customersById={customersById} customerByName={customerByName}
          onSalesPeriod={handleSalesPeriod} onSalesCustomDate={handleSalesCustomDate}
          onToggleSalesPicker={setSalesPickerOpen} onToggleShowAllSales={setShowAllSales}
          onCollPeriod={handleCollPeriod} onCollCustomDate={handleCollCustomDate} onToggleCollPicker={setCollPickerOpen}
          onGoToStaff={goToStaff} onSalesChartPeriod={setSalesChartPeriod} onOpenCustomer={openCustomer}
        />
      </main>

      {/* ── Customers page ── */}
      <main className="page" hidden={loading || !!error || view !== 'customers'}>
        <div className="lt">
          <p>{staffFilteredCustomers.length} with active balances</p>
          <h1>Customers</h1>
        </div>

        <label className="search">
          <Icon.search />
          <input
            type="search"
            placeholder="Search by name or phone"
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
            aria-label="Search customers"
          />
        </label>

        <div className="filters">
          <button aria-pressed={!staffFilter} onClick={() => setStaffFilter(null)}>Everyone</button>
          {staffSummary.map(s => (
            <button key={s.staff_name} aria-pressed={staffFilter === s.staff_name} onClick={() => setStaffFilter(s.staff_name)}>{s.staff_name}</button>
          ))}
        </div>

        {staffSubtotal && (
          <p className="sf" style={{ padding: '0 2px 10px' }}>
            <strong>{staffSubtotal.count}</strong> customers · <strong>{formatCompact(staffSubtotal.total)}</strong> present due
          </p>
        )}

        <CustomersList customers={filteredCustomers} highlightedId={highlightedId} onOpen={openCustomer} />
        <p className="sf">{filteredCustomers.length} of {staffFilteredCustomers.length} customers{staffFilter && ` · ${staffFilter}`}</p>
      </main>

      <div className="edge-bottom" aria-hidden="true"></div>

      <nav className="tabbar" aria-label="Sections">
        <div className="tabbar-tabs glass">
          <button aria-pressed={view === 'overview'} onClick={() => setView('overview')}><Icon.chart />Overview</button>
          <button aria-pressed={view === 'customers'} onClick={() => setView('customers')}><Icon.people />Customers</button>
        </div>
        <button className="glass circle" aria-label="Search customers or staff" onClick={() => setSearchOpen(true)}>
          <Icon.search />
        </button>
      </nav>

      {/* ── Global search overlay ── */}
      {searchOpen && (
        <>
          <div className="scrim" onClick={() => setSearchOpen(false)} />
          <section className="sheet" role="dialog" aria-modal="true" style={{ borderRadius: 28 }}>
            <div className="grab"></div>
            <div className="shead">
              <div><h3>Search</h3><p>Customers or staff</p></div>
              <button className="glass circle" onClick={() => setSearchOpen(false)} aria-label="Close"><Icon.close /></button>
            </div>
            <label className="search">
              <Icon.search />
              <input
                autoFocus
                type="search"
                placeholder="Search customers or staff…"
                value={globalSearch}
                onChange={e => setGlobalSearch(e.target.value)}
                aria-label="Search customers or staff"
              />
            </label>
            {searchResults && (
              searchResults.items.length === 0 ? (
                <div className="empty">No matches</div>
              ) : (
                <>
                  <p className="sf" style={{ padding: '0 2px 8px' }}>
                    {searchResults.type === 'staff'
                      ? `${searchResults.items.length} customers · ${searchResults.staff}`
                      : `${searchResults.items.length} result${searchResults.items.length === 1 ? '' : 's'}`}
                  </p>
                  <div className="list">
                    {searchResults.items.map(c => {
                      const color = STAFF_COLORS[c.assigned_to_name] || NEUTRAL_COLOR
                      return (
                        <button className="item" key={c.id} onClick={() => handleSearchResult(c)}>
                          <span className="av" style={{ background: color.bg, color: color.fg }}>{c.customer_name[0].toUpperCase()}</span>
                          <span className="body">
                            <div className="t1">{c.customer_name}</div>
                            {searchResults.type !== 'staff' && <div className="t2">{c.assigned_to_name || 'Unassigned'}</div>}
                          </span>
                          <span className="val"><div className="v">{c.present_pending !== 0 ? formatCompact(c.present_pending) : '—'}</div></span>
                        </button>
                      )
                    })}
                  </div>
                </>
              )
            )}
          </section>
        </>
      )}

      {/* ── Customer detail sheet ── */}
      <div className="scrim" hidden={!selectedCustomer} onClick={closeSheet} />
      <section
        className="sheet"
        role="dialog"
        aria-modal="true"
        hidden={!selectedCustomer}
        style={selectedCustomer ? {
          transform: sheetDragY ? `translateY(${sheetDragY}px)` : undefined,
          transition: sheetDragging.current ? 'none' : 'transform .25s cubic-bezier(.2,.9,.25,1)',
        } : undefined}
      >
        {selectedCustomer && (
          <>
            <div
              className="sheet-drag-handle"
              onTouchStart={handleSheetDragStart}
              onTouchMove={handleSheetDragMove}
              onTouchEnd={handleSheetDragEnd}
              onTouchCancel={handleSheetDragEnd}
            >
              <div className="grab"></div>
              <div className="shead">
                <div>
                  <h3>{selectedCustomer.customer_name}</h3>
                  <p>
                    {selectedCustomer.assigned_to_name || 'Unassigned'}
                    {selectedCustomer.customer_type === 'cash' ? ', cash customer' : `, ${selectedCustomer.credit_days || '—'} day credit`}
                    {selectedCustomer.flagged && ` · ${selectedCustomer.flagged_reason}`}
                  </p>
                </div>
                <button className="glass circle" onClick={closeSheet} aria-label="Close"><Icon.close /></button>
              </div>
            </div>

            {custDetailLoading ? (
              <div className="state-msg">Loading…</div>
            ) : custDetail ? (
              <>
                {cdActiveBills.length > 0 && (
                  <section className="card">
                    <div className="mhead"><span className="mlabel mlabel--red">Outstanding</span></div>
                    <div className="mval" style={{ marginTop: 9 }}><span className="big xl">{formatINR(cdTotalPending)}</span></div>
                    <div className="exact">
                      <b>{cdActiveBills.length}</b> {cdActiveBills.length === 1 ? 'bill' : 'bills'}
                      {cdOldestBill && <> · oldest {new Date(cdOldestBill.invoice_date + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' })}</>}
                    </div>
                  </section>
                )}

                <div className="stats">
                  <div className="stat"><div className="k">Purchased (FY)</div><div className="n">{cdFyTotal > 0 ? formatCompact(cdFyTotal) : '—'}</div></div>
                  <div className="stat"><div className="k">Avg order</div><div className="n">{cdAvgOrder > 0 ? formatCompact(Math.round(cdAvgOrder)) : '—'}</div></div>
                  <div className="stat"><div className="k">Last order</div><div className="n">{cdDaysSince !== null ? `${cdDaysSince}d ago` : '—'}</div></div>
                </div>

                {(custCallHref || custWaHref) && (
                  <div className={'actions' + (!custCallHref || !custWaHref ? ' actions--single' : '')}>
                    {custCallHref && <a className="btn tint-blue" href={custCallHref}><Icon.call />Call</a>}
                    {custWaHref && <a className="btn fill-green" href={custWaHref} target="_blank" rel="noreferrer"><Icon.wa />Send reminder</a>}
                  </div>
                )}

                {cdRating && (
                  <section className="card" style={{ marginTop: 12 }}>
                    <div className="mhead"><span className="mlabel">Customer rating</span></div>
                    <div className="rating-row" style={{ marginTop: 10 }}>
                      <div className="rating-stars">
                        {[1, 2, 3, 4, 5].map(i => <span key={i} className={'rating-star' + (i <= cdRating.stars ? ' rating-star--on' : '')} />)}
                      </div>
                      <div className="rating-reason">{cdRating.reason}</div>
                    </div>
                  </section>
                )}

                {cdHistory.length > 0 && (
                  <section className="card" style={{ marginTop: 12 }}>
                    <div className="chead">
                      <div className="clabel" style={{ marginTop: 0 }}>Monthly purchases (this FY)</div>
                    </div>
                    <div className="chart-wrap">
                      <ResponsiveContainer width="100%" height={110}>
                        <BarChart data={cdChartData} margin={{ top: 4, right: 0, left: 0, bottom: 0 }}>
                          <CartesianGrid vertical={false} stroke={chartPalette.grid} />
                          <XAxis dataKey="date" tick={{ fill: chartPalette.text, fontSize: 10 }} axisLine={{ stroke: chartPalette.grid }} tickLine={false} interval={0} />
                          <YAxis hide />
                          <Tooltip cursor={{ fill: 'rgba(48,176,199,0.08)' }} formatter={v => [formatINR(v), 'Purchases']} contentStyle={{ background: chartPalette.tooltipBg, border: `1px solid ${chartPalette.tooltipBorder}`, borderRadius: 10, fontSize: 12 }} />
                          <Bar dataKey="total" fill={chartPalette.bar} radius={[3, 3, 0, 0]} maxBarSize={40} />
                        </BarChart>
                      </ResponsiveContainer>
                    </div>
                  </section>
                )}

                {cdHistory.length > 0 && (
                  <div style={{ marginTop: 12 }}>
                    <div className="sh"><h2 style={{ fontSize: 15 }}>Purchase history</h2></div>
                    <div className="list">
                      {cdHistory.slice(0, 20).map((h, i) => (
                        <div className="item" key={i}>
                          <span className="body">
                            <div className="t1">{new Date(h.sale_date + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' })}</div>
                            <div className="t2">{h.voucher_number || '—'}</div>
                          </span>
                          <span className="val"><div className="v">{h.amount > 0 ? formatINR(h.amount) : '—'}</div></span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {cdHistory.length === 0 && cdActiveBills.length === 0 && (
                  <p className="msub" style={{ paddingTop: 8 }}>No purchase history or outstanding bills on record for this FY.</p>
                )}
              </>
            ) : null}
          </>
        )}
      </section>
    </div>
  )
}
