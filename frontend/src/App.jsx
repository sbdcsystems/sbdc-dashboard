import { useEffect, useState, useRef, useMemo } from 'react'
import {
  BarChart, Bar, XAxis, YAxis, Tooltip,
  ResponsiveContainer, CartesianGrid, Cell,
} from 'recharts'
import { supabase } from './lib/supabaseClient'
import './App.css'

// Bucket config
const BUCKET_ORDER = ['0-30', '30-60', '60-90', '90-120', '120+']
// Colour escalates from teal (recent/ok) → red (severely overdue)
const BUCKET_COLORS = ['#1C7A5C', '#B07A2E', '#C9603A', '#C9603A', '#A6342A']

// Hardcoded per known staff — falls back to neutral for Unassigned / any future name
const STAFF_COLORS = {
  'Venkatesh':    { bg: '#E5EBF4', fg: '#2C4A7C', bar: '#2C4A7C' },
  'Thiagarajan':  { bg: '#FAEAE2', fg: '#C9603A', bar: '#C9603A' },
  'Gowtham':      { bg: '#F7EEDD', fg: '#B07A2E', bar: '#B07A2E' },
  'Vijaya Priya': { bg: '#EFE7F3', fg: '#6B4C8A', bar: '#6B4C8A' },
}
const NEUTRAL_COLOR  = { bg: '#F0EEE7', fg: '#A3A19A', bar: '#A3A19A' }
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

// ── Card date-nav helpers ──────────────────────────────────────────────────

function _cardRange(period) {
  if (period === 'yesterday') {
    const d = _istNow(); d.setUTCDate(d.getUTCDate() - 1); const y = _fmtDate(d)
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

function formatCompact(amount) {
  if (amount >= 1e7) return '₹' + (amount / 1e7).toFixed(2) + ' Cr'
  if (amount >= 1e5) return '₹' + (amount / 1e5).toFixed(1) + ' L'
  if (amount >= 1e3) return '₹' + (amount / 1e3).toFixed(1) + ' K'
  return '₹' + Math.round(amount)
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
  return <span className="hero-figure">{formatINR(animated)}</span>
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
  console.log('[fetchCustHistory] INPUT — custName:', custName, '| custId:', custId)

  const base = () =>
    supabase.from('sales_history')
      .select('sale_date, voucher_number, amount')
      .gte('sale_date', FY_START)
      .order('sale_date', { ascending: false })
      .limit(200)

  // Run UUID-stamped rows and null-UUID name rows in parallel.
  // UUID rows are the reliable anchor; null-UUID rows catch records synced
  // before UUID stamping was added (April/May history gap).
  const [uuidResp, nameResp] = await Promise.all([
    custId ? base().eq('customer_id', custId) : Promise.resolve({ data: [] }),
    base().is('customer_id', null).ilike('customer_name', custName),
  ])
  console.log('[fetchCustHistory] UUID query — rows:', uuidResp.data?.length ?? 0, '| error:', uuidResp.error, '| data:', uuidResp.data)
  console.log('[fetchCustHistory] Name query (null-UUID) — rows:', nameResp.data?.length ?? 0, '| error:', nameResp.error, '| data:', nameResp.data)

  const combined = _dedupHistory([...(uuidResp.data || []), ...(nameResp.data || [])])
  console.log('[fetchCustHistory] Combined after dedup — rows:', combined.length, '| data:', combined)
  if (combined.length > 0) return combined

  // Fuzzy fallback: strip common suffixes and prefix-search null-UUID rows
  const stem = custName
    .replace(/[\s,.]*\b(pvt\.?\s*ltd\.?|private\s+limited|limited|ltd\.?|&\s*co\.?|and\s+co\.?|company|traders?|trading|mills?|industries|enterprises?|exports?|works?|dyers?|textiles?)\s*\.?\s*$/i, '')
    .trim()
  console.log('[fetchCustHistory] Fuzzy stem:', stem)
  if (stem.length >= 3 && stem.toLowerCase() !== custName.toLowerCase()) {
    const { data: fuzzy, error: fuzzyErr } = await base().is('customer_id', null).ilike('customer_name', `${stem}%`)
    console.log('[fetchCustHistory] Fuzzy query — rows:', fuzzy?.length ?? 0, '| error:', fuzzyErr, '| data:', fuzzy)
    const withFuzzy = _dedupHistory([...(uuidResp.data || []), ...(fuzzy || [])])
    console.log('[fetchCustHistory] Combined with fuzzy — rows:', withFuzzy.length, '| data:', withFuzzy)
    if (withFuzzy.length > 0) return withFuzzy
  }

  console.log('[fetchCustHistory] FINAL: returning UUID rows only — rows:', uuidResp.data?.length ?? 0)
  return uuidResp.data || []
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
  const [prevView, setPrevView]                   = useState('customers')
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

  // Auto-clear row highlight after 2.5 s
  useEffect(() => {
    if (!highlightedId) return
    const t = setTimeout(() => setHighlightedId(null), 2500)
    return () => clearTimeout(t)
  }, [highlightedId])

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
        ] = await Promise.all([
          supabase.from('outstanding_status_summary').select('*'),
          supabase.from('outstanding_bucket_summary').select('*'),
          supabase.from('flagged_customers_summary').select('*'),
          supabase.from('outstanding_by_staff_summary').select('*'),
          supabase.from('daily_sales').select('*').eq('sale_date', TODAY).maybeSingle(),
          supabase.from('daily_collections').select('*').eq('sale_date', TODAY).maybeSingle(),
        ])

        if (e1) throw e1
        if (e2) throw e2
        if (e3) throw e3
        if (e4) throw e4
        // e6 is non-fatal — table may not exist yet

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

  function goToStaff(name) {
    setView('customers')
    setStaffFilter(name)
    setSearchQuery('')
  }

  const staffFilteredCustomers = customers.filter(c => {
    if (!staffFilter) return true
    if (staffFilter === 'Unassigned') return !c.assigned_to_name
    return c.assigned_to_name === staffFilter
  })

  const filteredCustomers = staffFilteredCustomers.filter(c => {
    const q = searchQuery.trim().toLowerCase()
    if (!q) return true
    return (
      c.customer_name.toLowerCase().includes(q) ||
      (c.phone || '').includes(q)
    )
  })

  const staffSubtotal = staffFilter ? {
    count: staffFilteredCustomers.length,
    total: staffFilteredCustomers.reduce((sum, c) => sum + c.present_pending, 0),
  } : null

  const maxStaffPending = staffSummary.length
    ? Math.max(...staffSummary.map(s => s.total_pending), 1)
    : 1

  const presentRatio = summary && (summary.recentTotal + summary.staleTotal) > 0
    ? (summary.recentTotal / (summary.recentTotal + summary.staleTotal)) * 100
    : 50

  // ── Sales history aggregates ────────────────────────────────────────────────
  const periodStart = salesChartPeriod === 'last_month' ? LAST_MONTH_START
    : salesChartPeriod === 'fy' ? FY_START
    : MONTH_START
  const periodEnd = salesChartPeriod === 'last_month' ? LAST_MONTH_END : TODAY

  const periodSales = salesHistory.filter(s => s.sale_date >= periodStart && s.sale_date <= periodEnd)
  const periodTotal = periodSales.reduce((sum, s) => sum + s.amount, 0)
  const periodCount = periodSales.length

  let chartData
  if (salesChartPeriod === 'fy') {
    const fyMonths = []
    const cur = new Date(FY_START + 'T00:00:00')
    const end = new Date(TODAY + 'T00:00:00')
    while (cur <= end) {
      fyMonths.push({ key: _fmtDate(new Date(cur)).slice(0, 7), label: cur.toLocaleString('en-IN', { month: 'short' }) })
      cur.setMonth(cur.getMonth() + 1)
    }
    const mmap = {}
    salesHistory.forEach(s => { const m = s.sale_date.slice(0, 7); mmap[m] = (mmap[m] || 0) + s.amount })
    chartData = fyMonths.map(({ key, label }) => ({ date: label, total: mmap[key] || 0 }))
  } else {
    const days = []
    const cur = new Date(periodStart + 'T00:00:00')
    const end = new Date(periodEnd + 'T00:00:00')
    while (cur <= end) { days.push(_fmtDate(new Date(cur))); cur.setDate(cur.getDate() + 1) }
    const dmap = {}
    periodSales.forEach(s => { dmap[s.sale_date] = (dmap[s.sale_date] || 0) + s.amount })
    chartData = days.map(d => {
      const dd = new Date(d + 'T00:00:00')
      return { date: `${dd.getDate()}/${dd.getMonth() + 1}`, total: dmap[d] || 0 }
    })
  }

  const topCustomerMap = {}
  periodSales.forEach(s => {
    const k = s.customer_name || 'Unknown'
    topCustomerMap[k] = (topCustomerMap[k] || 0) + s.amount
  })
  const topCustomers   = Object.entries(topCustomerMap)
    .sort((a, b) => b[1] - a[1]).slice(0, 5)
    .map(([name, total]) => ({ name, total }))
  const maxTopCustomer = topCustomers.length ? Math.max(...topCustomers.map(c => c.total), 1) : 1

  // UUID-keyed maps (primary — no string matching, immune to name variations)
  const staffById      = Object.fromEntries(customers.map(c => [c.id, c.assigned_to_name || 'Unassigned']))
  const typeById       = Object.fromEntries(customers.map(c => [c.id, { type: c.customer_type, credit_days: c.credit_days }]))
  const customersById  = Object.fromEntries(customers.map(c => [c.id, c]))
  // Name-keyed maps (fallback — for items synced before UUID enrichment was added)
  const staffByName    = Object.fromEntries(customers.map(c => [c.customer_name.trim().toLowerCase(), c.assigned_to_name || 'Unassigned']))
  const typeByName     = Object.fromEntries(customers.map(c => [c.customer_name.trim().toLowerCase(), { type: c.customer_type, credit_days: c.credit_days }]))
  const customerByName = Object.fromEntries(customers.map(c => [c.customer_name.trim().toLowerCase(), c]))

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
      items: customers.filter(c => c.customer_name.toLowerCase().includes(q)).slice(0, 12),
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
    const fyMonths = []
    const cur = new Date(FY_START + 'T00:00:00')
    const end = new Date(TODAY + 'T00:00:00')
    while (cur <= end) {
      fyMonths.push({ key: _fmtDate(new Date(cur)).slice(0, 7), label: cur.toLocaleString('en-IN', { month: 'short' }) })
      cur.setMonth(cur.getMonth() + 1)
    }
    const mmap = {}
    custDetail.history.forEach(s => { const m = s.sale_date.slice(0, 7); mmap[m] = (mmap[m] || 0) + s.amount })
    return fyMonths.map(({ key, label }) => ({ date: label, total: mmap[key] || 0 }))
  }, [custDetail])

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

  async function handleSalesPeriod(period) {
    setSalesCardPeriod(period)
    if (period !== 'custom') setSalesPickerOpen(false)
    if (period === 'today') { setSalesCardRows(null); return }
    const { from, to } = _cardRange(period)
    setSalesCardLoading(true)
    const { data } = await supabase.from('daily_sales').select('*').gte('sale_date', from).lte('sale_date', to)
    setSalesCardRows(data || [])
    setSalesCardLoading(false)
  }

  async function handleSalesCustomDate(dateStr) {
    setSalesCustomDate(dateStr)
    if (!dateStr) return
    setSalesCardPeriod('custom')
    setSalesCardLoading(true)
    const { data } = await supabase.from('daily_sales').select('*').eq('sale_date', dateStr)
    setSalesCardRows(data || [])
    setSalesCardLoading(false)
  }

  async function handleCollPeriod(period) {
    setCollCardPeriod(period)
    if (period !== 'custom') setCollPickerOpen(false)
    if (period === 'today') { setCollCardRows(null); return }
    const { from, to } = _cardRange(period)
    setCollCardLoading(true)
    const { data } = await supabase.from('daily_collections').select('*').gte('sale_date', from).lte('sale_date', to)
    setCollCardRows(data || [])
    setCollCardLoading(false)
  }

  async function handleCollCustomDate(dateStr) {
    setCollCustomDate(dateStr)
    if (!dateStr) return
    setCollCardPeriod('custom')
    setCollCardLoading(true)
    const { data } = await supabase.from('daily_collections').select('*').eq('sale_date', dateStr)
    setCollCardRows(data || [])
    setCollCardLoading(false)
  }

  function handleSearchResult(customer) {
    setGlobalSearch('')
    setSearchOpen(false)
    setView('customers')
    setStaffFilter(null)
    setSearchQuery('')
    setHighlightedId(customer.id)
  }

  async function openCustomer(customer, fromView) {
    setSelectedCustomer(customer)
    setPrevView(fromView)
    setCustDetail(null)
    setCustDetailLoading(true)
    setView('customer-detail')
    const [{ data: bills }, history] = await Promise.all([
      supabase.from('outstanding')
        .select('invoice_date, invoice_ref, pending_amount, days_overdue, age_status, bucket')
        .eq('customer_id', customer.id)
        .order('invoice_date', { ascending: true }),
      fetchCustHistory(customer.customer_name, customer.id),
    ])
    setCustDetail({ bills: bills || [], history })
    setCustDetailLoading(false)
  }

  const salesDisplayData = salesCardRows !== null ? _mergeCardRows(salesCardRows) : todaySales
  const collDisplayData  = collCardRows  !== null ? _mergeCardRows(collCardRows)  : todayCollections

  const sortedSalesItems       = [...(salesDisplayData?.items ?? [])].sort((a, b) => b.amount - a.amount)
  const visibleSalesItems      = showAllSales ? sortedSalesItems : sortedSalesItems.slice(0, 10)
  const salesHasDetail         = sortedSalesItems.some(i => i.customer_name || i.invoice_ref)
  const sortedCollectionItems  = [...(collDisplayData?.items ?? [])].sort((a, b) => b.amount - a.amount)
  const collectionsHasDetail   = sortedCollectionItems.some(i => i.customer_name || i.invoice_ref)

  // ── Customer detail derived values ─────────────────────────────────────────
  const cdBills         = custDetail?.bills   ?? []
  const cdHistory       = custDetail?.history ?? []
  const cdActiveBills   = cdBills.filter(b => b.pending_amount > 0)
  const cdTotalPending  = cdActiveBills.reduce((s, b) => s + Number(b.pending_amount), 0)
  const cdOldestBill    = cdActiveBills[0] ?? null
  const cdFyTotal       = cdHistory.reduce((s, h) => s + Number(h.amount), 0)
  const cdAvgOrder      = cdHistory.length > 0 ? cdFyTotal / cdHistory.length : 0
  const cdLastPurchase  = cdHistory[0]?.sale_date ?? null
  const cdDaysSince     = cdLastPurchase
    ? Math.floor((new Date(TODAY) - new Date(cdLastPurchase + 'T00:00:00')) / 86400000)
    : null
  const cdRating        = custDetail ? computeRating(cdBills, cdHistory, fyMedian, FY_MONTHS_ELAPSED) : null
  const cdRecentHistory = cdHistory.slice(0, 20)

  return (
    <div className="app">
      <div className="frame">

        {/* ── Header ── */}
        <header className="topbar">
          <div>
            <div className="brand">Supreme Balaji Dye Chem</div>
            <div className="brand-sub">Collections dashboard</div>
          </div>

          {/* Global search */}
          <div className="gsearch-wrap">
            <input
              className="gsearch-input"
              placeholder="Search customers or staff…"
              value={globalSearch}
              onChange={e => { setGlobalSearch(e.target.value); setSearchOpen(true) }}
              onFocus={() => setSearchOpen(true)}
              onBlur={() => setTimeout(() => setSearchOpen(false), 160)}
            />
            {searchOpen && searchResults && (
              <div className="gsearch-dropdown">
                {searchResults.items.length === 0 ? (
                  <div className="gsearch-empty">No matches</div>
                ) : (
                  <>
                    <div className="gsearch-header">
                      {searchResults.type === 'staff'
                        ? `${searchResults.items.length} customers · ${searchResults.staff}`
                        : `${searchResults.items.length} result${searchResults.items.length === 1 ? '' : 's'}`}
                    </div>
                    {searchResults.items.map(c => {
                      const color = STAFF_COLORS[c.assigned_to_name] || NEUTRAL_COLOR
                      return (
                        <div
                          key={c.id}
                          className="gsearch-result"
                          onMouseDown={() => handleSearchResult(c)}
                        >
                          <div className="avatar avatar--sm" style={{ background: color.bg, color: color.fg, flexShrink: 0 }}>
                            {c.customer_name[0].toUpperCase()}
                          </div>
                          <div className="gsearch-result__name">{c.customer_name}</div>
                          {c.assigned_to_name && searchResults.type !== 'staff' && (
                            <div className="gsearch-result__staff" style={{ color: color.fg }}>
                              {c.assigned_to_name}
                            </div>
                          )}
                          <span className={'pill ' + (c.customer_type === 'cash' ? 'pill--cash' : 'pill--credit')}>
                            {c.customer_type === 'cash' ? 'Cash' : `${c.credit_days || '—'}d`}
                          </span>
                          <div className="gsearch-result__amt">
                            {c.present_pending > 0 ? formatCompact(c.present_pending) : '—'}
                          </div>
                        </div>
                      )
                    })}
                  </>
                )}
              </div>
            )}
          </div>

          <div className="tabs">
            <button
              className={'tab' + (view === 'overview' || (view === 'customer-detail' && prevView === 'overview') ? ' tab--active' : '')}
              onClick={() => setView('overview')}
            >
              Overview
            </button>
            <button
              className={'tab' + (view === 'customers' || (view === 'customer-detail' && prevView === 'customers') ? ' tab--active' : '')}
              onClick={() => setView('customers')}
            >
              Customers
            </button>
          </div>
        </header>

        {loading && <div className="state-msg">Loading...</div>}
        {error   && <div className="state-msg state-msg--error">Error: {error}</div>}

        {/* ── Overview tab ── */}
        {!loading && !error && summary && view === 'overview' && (
          <>
            {/* Hero */}
            <section className="hero">
              <div className="eyebrow">
                <span className="eyebrow__dot" />
                Active &amp; recent
              </div>
              <HeroFigure amount={summary.recentTotal} />
              <p className="hero-sub">
                Across <strong>{summary.recentCount} customers</strong> with active, recent outstanding balances
              </p>
            </section>

            {/* Today's Sales */}
            <div className="card" style={{ marginBottom: 28 }}>
              <div className="card-head">
                <span className="chip chip--teal" />
                <span className="card-label">Today's sales</span>
              </div>

              <div className="card-nav">
                {[['today','Today'],['yesterday','Yesterday'],['this_week','This week'],['this_month','This month']].map(([p, lbl]) => (
                  <button key={p} className={'card-nav-btn' + (salesCardPeriod === p ? ' card-nav-btn--active' : '')} onClick={() => handleSalesPeriod(p)}>{lbl}</button>
                ))}
                <button
                  className={'card-nav-btn' + (salesCardPeriod === 'custom' ? ' card-nav-btn--active' : '')}
                  onClick={() => setSalesPickerOpen(v => !v)}
                >
                  {salesCardPeriod === 'custom' && salesCustomDate
                    ? new Date(salesCustomDate + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })
                    : salesPickerOpen ? 'Less ▲' : 'More ▾'}
                </button>
                {salesPickerOpen && (
                  <input
                    type="date"
                    className="card-nav-datepicker"
                    value={salesCustomDate}
                    max={TODAY}
                    onChange={e => handleSalesCustomDate(e.target.value)}
                  />
                )}
              </div>

              {salesCardLoading ? (
                <div className="card-skeleton">
                  <div className="skeleton skeleton--figure" />
                  <div className="skeleton skeleton--line" />
                </div>
              ) : (
                <div key={salesCardPeriod + salesCustomDate} className="card-fade">
                  {salesDisplayData ? (
                    <>
                      <span className="sales-figure">{formatINR(salesDisplayData.total_amount)}</span>
                      <p className="hero-sub" style={{ marginTop: 8 }}>
                        <strong>{salesDisplayData.invoice_count}</strong>{' '}
                        {salesDisplayData.invoice_count === 1 ? 'invoice' : 'invoices'}
                        {['today', 'yesterday', 'custom'].includes(salesCardPeriod) && salesDisplayData.synced_at && (
                          <>&nbsp;&middot;&nbsp;synced {new Date(salesDisplayData.synced_at).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' })}</>
                        )}
                      </p>
                      <div style={{ borderTop: '1px solid var(--border-soft)', margin: '18px 0 14px' }} />
                      {salesHasDetail ? (
                        <>
                          <table className="cust-table">
                            <thead>
                              <tr>
                                <th>Customer</th>
                                <th className="mobile-hide">Invoice ref</th>
                                <th>Handled by</th>
                                <th className="mobile-hide">Type</th>
                                <th className="r">Amount</th>
                              </tr>
                            </thead>
                            <tbody>
                              {visibleSalesItems.map((item, idx) => {
                                const cid      = item.customer_id
                                const nameKey  = item.customer_name?.trim().toLowerCase()
                                const staff    = (cid != null ? staffById[cid]  : undefined) ?? staffByName[nameKey] ?? 'Unassigned'
                                const typeInfo = (cid != null ? typeById[cid]   : undefined) ?? typeByName[nameKey]
                                const cust     = (cid != null ? customersById[cid] : null) ?? customerByName[nameKey]
                                return (
                                  <tr key={idx}>
                                    <td>
                                      {cust
                                        ? <span className="cust-name--link" onClick={() => openCustomer(cust, 'overview')}>{item.customer_name || '—'}</span>
                                        : (item.customer_name || '—')}
                                    </td>
                                    <td className="mobile-hide" style={{ fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--text-secondary)' }}>
                                      {item.invoice_ref || '—'}
                                    </td>
                                    <td>{staff === 'Unassigned'
                                      ? <span className="pill pill--unassigned">Unassigned</span>
                                      : staff}
                                    </td>
                                    <td className="mobile-hide">
                                      {typeInfo?.type === 'cash'
                                        ? <span className="pill pill--cash">Cash</span>
                                        : typeInfo?.type === 'credit'
                                        ? <span className="pill pill--credit">Credit&nbsp;·&nbsp;{typeInfo.credit_days || '—'}d</span>
                                        : '—'}
                                    </td>
                                    <td className="r">{formatINR(item.amount)}</td>
                                  </tr>
                                )
                              })}
                            </tbody>
                          </table>
                          {sortedSalesItems.length > 10 && (
                            <button
                              className="cust-subtotal__clear"
                              style={{ marginTop: 12, display: 'block' }}
                              onClick={() => setShowAllSales(v => !v)}
                            >
                              {showAllSales ? 'Show top 10' : `Show all ${sortedSalesItems.length} invoices`}
                            </button>
                          )}
                        </>
                      ) : (
                        <p className="hero-sub">Per-invoice breakdown not available from this Tally export</p>
                      )}
                    </>
                  ) : (
                    <p className="hero-sub">
                      {salesCardPeriod === 'today'
                        ? "No data yet — sync hasn't run today"
                        : `No data for ${_periodLabel(salesCardPeriod, salesCustomDate)}`}
                    </p>
                  )}
                </div>
              )}
            </div>

            {/* Collections */}
            <div className="card" style={{ marginBottom: 28 }}>
              <div className="card-head">
                <span className="chip chip--indigo" />
                <span className="card-label">Collections today</span>
              </div>

              <div className="card-nav">
                {[['today','Today'],['yesterday','Yesterday'],['this_week','This week'],['this_month','This month']].map(([p, lbl]) => (
                  <button key={p} className={'card-nav-btn' + (collCardPeriod === p ? ' card-nav-btn--active' : '')} onClick={() => handleCollPeriod(p)}>{lbl}</button>
                ))}
                <button
                  className={'card-nav-btn' + (collCardPeriod === 'custom' ? ' card-nav-btn--active' : '')}
                  onClick={() => setCollPickerOpen(v => !v)}
                >
                  {collCardPeriod === 'custom' && collCustomDate
                    ? new Date(collCustomDate + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })
                    : collPickerOpen ? 'Less ▲' : 'More ▾'}
                </button>
                {collPickerOpen && (
                  <input
                    type="date"
                    className="card-nav-datepicker"
                    value={collCustomDate}
                    max={TODAY}
                    onChange={e => handleCollCustomDate(e.target.value)}
                  />
                )}
              </div>

              {collCardLoading ? (
                <div className="card-skeleton">
                  <div className="skeleton skeleton--figure" />
                  <div className="skeleton skeleton--line" />
                </div>
              ) : (
                <div key={collCardPeriod + collCustomDate} className="card-fade">
                  {collDisplayData ? (
                    collDisplayData.invoice_count === 0 ? (
                      <p className="hero-sub">
                        {collCardPeriod === 'today'
                          ? 'No collections received today'
                          : `No collections received ${_periodLabel(collCardPeriod, collCustomDate)}`}
                      </p>
                    ) : (
                      <>
                        <span className="sales-figure">{formatINR(collDisplayData.total_amount)}</span>
                        <p className="hero-sub" style={{ marginTop: 8 }}>
                          <strong>{collDisplayData.invoice_count}</strong>{' '}
                          {collDisplayData.invoice_count === 1 ? 'receipt' : 'receipts'}
                          {['today', 'yesterday', 'custom'].includes(collCardPeriod) && collDisplayData.synced_at && (
                            <>&nbsp;&middot;&nbsp;synced {new Date(collDisplayData.synced_at).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' })}</>
                          )}
                        </p>
                        <div style={{ borderTop: '1px solid var(--border-soft)', margin: '18px 0 14px' }} />
                        {collectionsHasDetail ? (
                          <table className="cust-table">
                            <thead>
                              <tr>
                                <th>Customer</th>
                                <th className="mobile-hide">Receipt ref</th>
                                <th>Handled by</th>
                                <th className="r">Amount</th>
                              </tr>
                            </thead>
                            <tbody>
                              {sortedCollectionItems.map((item, idx) => {
                                const cid      = item.customer_id
                                const nameKey  = item.customer_name?.trim().toLowerCase()
                                const staff    = (cid != null ? staffById[cid] : undefined) ?? staffByName[nameKey] ?? 'Unassigned'
                                const cust     = (cid != null ? customersById[cid] : null) ?? customerByName[nameKey]
                                return (
                                  <tr key={idx}>
                                    <td>
                                      {cust
                                        ? <span className="cust-name--link" onClick={() => openCustomer(cust, 'overview')}>{item.customer_name || '—'}</span>
                                        : (item.customer_name || '—')}
                                    </td>
                                    <td className="mobile-hide" style={{ fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--text-secondary)' }}>
                                      {item.invoice_ref || '—'}
                                    </td>
                                    <td>{staff === 'Unassigned'
                                      ? <span className="pill pill--unassigned">Unassigned</span>
                                      : staff}
                                    </td>
                                    <td className="r">{formatINR(item.amount)}</td>
                                  </tr>
                                )
                              })}
                            </tbody>
                          </table>
                        ) : (
                          <p className="hero-sub">Per-receipt breakdown not available from this Tally export</p>
                        )}
                      </>
                    )
                  ) : (
                    <p className="hero-sub">
                      {collCardPeriod === 'today'
                        ? "No collections yet today — sync hasn't run"
                        : `No data for ${_periodLabel(collCardPeriod, collCustomDate)}`}
                    </p>
                  )}
                </div>
              )}
            </div>

            {/* Sales chart */}
            <div className="card" style={{ marginBottom: 16 }}>
              <div className="card-head">
                <span className="chip chip--teal" />
                <span className="card-label">Sales</span>
                <div className="period-switcher">
                  {[['this_month', 'This month'], ['last_month', 'Last month'], ['fy', 'This FY']].map(([p, label]) => (
                    <button
                      key={p}
                      className={'period-btn' + (salesChartPeriod === p ? ' period-btn--active' : '')}
                      onClick={() => setSalesChartPeriod(p)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>
              {salesHistory.length > 0 ? (
                <>
                  <span className="sales-figure">{formatINR(periodTotal)}</span>
                  <p className="hero-sub" style={{ marginTop: 8, marginBottom: 20 }}>
                    <strong>{periodCount}</strong> {periodCount === 1 ? 'invoice' : 'invoices'}
                  </p>
                  <ResponsiveContainer width="100%" height={130}>
                    <BarChart data={chartData} margin={{ top: 4, right: 0, left: 0, bottom: 0 }}>
                      <CartesianGrid vertical={false} stroke="#E7E4DC" />
                      <XAxis
                        dataKey="date"
                        tick={{ fill: '#6B6A64', fontSize: 11, fontFamily: 'Manrope, sans-serif' }}
                        axisLine={{ stroke: '#E7E4DC' }}
                        tickLine={false}
                        interval={salesChartPeriod === 'fy' ? 0 : 'preserveStartEnd'}
                      />
                      <YAxis hide />
                      <Tooltip
                        cursor={{ fill: 'rgba(28,122,92,0.06)' }}
                        formatter={v => [formatINR(v), 'Sales']}
                        contentStyle={{ background: '#fff', border: '1px solid #E7E4DC', borderRadius: 8, fontFamily: 'Manrope, sans-serif', fontSize: 13 }}
                      />
                      <Bar dataKey="total" fill="#1C7A5C" radius={[3, 3, 0, 0]} maxBarSize={salesChartPeriod === 'fy' ? 60 : 32} />
                    </BarChart>
                  </ResponsiveContainer>
                </>
              ) : (
                <p className="hero-sub">No data yet — sync hasn't run</p>
              )}
            </div>

            {/* Top customers this month */}
            {topCustomers.length > 0 && (
              <div className="card" style={{ marginBottom: 28 }}>
                <div className="card-head">
                  <span className="chip chip--teal" />
                  <span className="card-label">
                  Top customers {salesChartPeriod === 'fy' ? 'this FY' : salesChartPeriod === 'last_month' ? 'last month' : 'this month'}
                </span>
                </div>
                {topCustomers.map((c, i) => (
                  <div className="staff-row" key={i}>
                    <div className="avatar" style={{ background: '#E5F0EC', color: '#1C7A5C' }}>
                      {(c.name[0] || '?').toUpperCase()}
                    </div>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div className="staff-name" style={{ fontSize: 13, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        {c.name}
                      </div>
                    </div>
                    <div className="staff-bar-track">
                      <div
                        className="staff-bar-fill"
                        style={{ width: `${(c.total / maxTopCustomer) * 100}%`, background: '#1C7A5C' }}
                      />
                    </div>
                    <div className="staff-amt" style={{ color: '#1C7A5C' }}>
                      {formatCompact(c.total)}
                    </div>
                  </div>
                ))}
              </div>
            )}

            {/* Present vs archived | Staff dues */}
            <div className="two-col">
              <div className="card">
                <div className="card-head">
                  <span className="chip chip--teal" />
                  <span className="card-label">Present vs archived</span>
                </div>
                <div className="split-bar">
                  <div className="split-bar__present"  style={{ width: `${presentRatio}%` }} />
                  <div className="split-bar__archived" style={{ width: `${100 - presentRatio}%` }} />
                </div>
                <div className="split-legend">
                  <div className="split-legend__item">
                    <span className="dot dot--teal" />
                    <span>Present</span>
                    <span className="split-legend__amt split-legend__amt--teal">
                      {formatCompact(summary.recentTotal)}
                    </span>
                  </div>
                  <div className="split-legend__item">
                    <span className="dot dot--red" />
                    <span>Archived</span>
                    <span className="split-legend__amt split-legend__amt--red">
                      {formatCompact(summary.staleTotal)}
                    </span>
                  </div>
                </div>
              </div>

              <div className="card">
                <div className="card-head">
                  <span className="chip chip--indigo" />
                  <span className="card-label">Present dues by staff</span>
                </div>
                {staffSummary.map(s => {
                  const color  = STAFF_COLORS[s.staff_name] || NEUTRAL_COLOR
                  const barPct = (s.total_pending / maxStaffPending) * 100
                  return (
                    <div
                      className="staff-row staff-row--link"
                      key={s.staff_name}
                      onClick={() => goToStaff(s.staff_name)}
                      title={`View ${s.staff_name}'s customers`}
                    >
                      <div
                        className="avatar"
                        style={{ background: color.bg, color: color.fg }}
                      >
                        {getInitials(s.staff_name)}
                      </div>
                      <div className="staff-name-wrap">
                        <div className="staff-name">{s.staff_name}</div>
                        <div className="staff-count">{s.customer_count} active</div>
                      </div>
                      <div className="staff-bar-track">
                        <div
                          className="staff-bar-fill"
                          style={{ width: `${barPct}%`, background: color.bar }}
                        />
                      </div>
                      <div className="staff-amt" style={{ color: color.fg }}>
                        {formatCompact(s.total_pending)}
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>

            {/* Aging chart */}
            <div className="card card--chart">
              <div className="card-head">
                <span className="chip chip--indigo" />
                <span className="card-label">Aging of present dues</span>
              </div>
              <ResponsiveContainer width="100%" height={200}>
                <BarChart
                  data={summary.bucketChartData}
                  margin={{ top: 4, right: 0, left: 0, bottom: 0 }}
                >
                  <CartesianGrid vertical={false} stroke="#E7E4DC" />
                  <XAxis
                    dataKey="bucket"
                    tick={{ fill: '#6B6A64', fontSize: 12, fontFamily: 'Manrope, sans-serif' }}
                    axisLine={{ stroke: '#E7E4DC' }}
                    tickLine={false}
                  />
                  <YAxis hide />
                  <Tooltip
                    cursor={{ fill: 'rgba(44,74,124,0.05)' }}
                    formatter={(value) => [formatINR(value), 'Outstanding']}
                    labelFormatter={(label) => `${label} days overdue`}
                    contentStyle={{
                      background: '#fff',
                      border: '1px solid #E7E4DC',
                      borderRadius: 8,
                      fontFamily: 'Manrope, sans-serif',
                      fontSize: 13,
                    }}
                  />
                  <Bar dataKey="total" radius={[4, 4, 0, 0]} maxBarSize={56}>
                    {summary.bucketChartData.map((_, i) => (
                      <Cell key={i} fill={BUCKET_COLORS[i] || BUCKET_COLORS[0]} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>

            {/* Flagged accounts */}
            {flagged.length > 0 && (
              <div className="card card--flagged">
                <div className="card-head">
                  <span className="chip chip--red" />
                  <span className="card-label card-label--red">
                    Flagged accounts with outstanding dues
                  </span>
                </div>
                {flagged.map(c => (
                  <div className="flagged-row" key={c.id}>
                    <div>
                      <div
                        className={'flagged-row__name' + (customersById[c.id] ? ' cust-name--link' : '')}
                        onClick={() => { const cust = customersById[c.id]; if (cust) openCustomer(cust, 'overview') }}
                      >
                        {c.customer_name}
                      </div>
                      <div className="flagged-row__reason">{c.flagged_reason}</div>
                    </div>
                    <div className="flagged-row__amt">{formatINR(c.total_pending)}</div>
                  </div>
                ))}
              </div>
            )}
          </>
        )}

        {/* ── Customers tab ── */}
        {!loading && !error && view === 'customers' && (
          <div className="card">
            <div className="card-head">
              <span className="chip chip--indigo" />
              <span className="card-label">
                {filteredCustomers.length} of {staffFilteredCustomers.length} customers
                {staffFilter && ` · ${staffFilter}`}
              </span>
            </div>

            {/* Staff filter pills */}
            <div className="staff-filter">
              <button
                className={'staff-filter-btn' + (!staffFilter ? ' staff-filter-btn--active' : '')}
                onClick={() => setStaffFilter(null)}
              >
                All
              </button>
              {staffSummary.map(s => {
                const color    = STAFF_COLORS[s.staff_name] || NEUTRAL_COLOR
                const isActive = staffFilter === s.staff_name
                return (
                  <button
                    key={s.staff_name}
                    className={'staff-filter-btn' + (isActive ? ' staff-filter-btn--active' : '')}
                    style={isActive
                      ? { background: color.bg, color: color.fg, borderColor: color.fg }
                      : {}}
                    onClick={() => setStaffFilter(s.staff_name)}
                  >
                    {s.staff_name}
                  </button>
                )
              })}
            </div>

            {/* Subtotal row when a staff filter is active */}
            {staffSubtotal && (
              <div className="cust-subtotal">
                <span className="cust-subtotal__label">{staffFilter}</span>
                <span className="cust-subtotal__stat">{staffSubtotal.count} customers</span>
                <span className="cust-subtotal__sep">·</span>
                <span className="cust-subtotal__stat">{formatCompact(staffSubtotal.total)} present due</span>
                <button className="cust-subtotal__clear" onClick={() => setStaffFilter(null)}>
                  All customers
                </button>
              </div>
            )}

            <input
              className="search-input"
              placeholder="Search by name or phone..."
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
            />
            <table className="cust-table">
              <thead>
                <tr>
                  <th>Customer</th>
                  <th className="mobile-hide">Assigned to</th>
                  <th className="mobile-hide">Terms</th>
                  <th className="r">Present due</th>
                  <th className="r mobile-hide">Phone</th>
                </tr>
              </thead>
              <tbody>
                {filteredCustomers.map(c => (
                  <tr
                    key={c.id}
                    className={c.id === highlightedId ? 'cust-row--highlight' : ''}
                    ref={c.id === highlightedId
                      ? el => el && el.scrollIntoView({ behavior: 'smooth', block: 'center' })
                      : null}
                  >
                    <td>
                      <div className="cust-cell">
                        <div
                          className="avatar avatar--sm"
                          style={{
                            background: c.flagged ? '#F7E5E3' : '#E5EBF4',
                            color:      c.flagged ? '#7C271F' : '#2C4A7C',
                          }}
                        >
                          {c.customer_name[0].toUpperCase()}
                        </div>
                        <div>
                          <div className="cust-name cust-name--link" onClick={() => openCustomer(c, 'customers')}>{c.customer_name}</div>
                          {c.flagged && (
                            <div className="cust-meta">{c.flagged_reason}</div>
                          )}
                        </div>
                      </div>
                    </td>
                    <td className="mobile-hide">
                      {c.assigned_to_name
                        ? c.assigned_to_name
                        : <span className="pill pill--unassigned">Unassigned</span>}
                    </td>
                    <td className="mobile-hide">
                      {c.customer_type === 'cash'
                        ? <span className="pill pill--cash">Cash</span>
                        : <span className="pill pill--credit">
                            Credit&nbsp;·&nbsp;{c.credit_days || '—'}d
                          </span>}
                    </td>
                    <td className="r">
                      {c.present_pending > 0 ? formatINR(c.present_pending) : '—'}
                    </td>
                    <td className="r mobile-hide">{c.phone || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* ── Customer detail view ── */}
        {!loading && !error && view === 'customer-detail' && selectedCustomer && (
          <div>
            <button className="detail-back" onClick={() => setView(prevView)}>
              ← Back to {prevView === 'overview' ? 'overview' : 'customers'}
            </button>

            {/* Header */}
            <div className="card" style={{ marginBottom: 16 }}>
              <div className="detail-header">
                <div
                  className="avatar detail-avatar"
                  style={{
                    background: selectedCustomer.flagged
                      ? '#F7E5E3'
                      : (STAFF_COLORS[selectedCustomer.assigned_to_name]?.bg || '#E5EBF4'),
                    color: selectedCustomer.flagged
                      ? '#7C271F'
                      : (STAFF_COLORS[selectedCustomer.assigned_to_name]?.fg || '#2C4A7C'),
                  }}
                >
                  {selectedCustomer.customer_name[0].toUpperCase()}
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="detail-name">{selectedCustomer.customer_name}</div>
                  {selectedCustomer.flagged && (
                    <div className="cust-meta">{selectedCustomer.flagged_reason}</div>
                  )}
                </div>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexShrink: 0, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                  {selectedCustomer.assigned_to_name
                    ? (() => {
                        const color = STAFF_COLORS[selectedCustomer.assigned_to_name] || NEUTRAL_COLOR
                        return <span className="pill" style={{ background: color.bg, color: color.fg }}>{selectedCustomer.assigned_to_name}</span>
                      })()
                    : <span className="pill pill--unassigned">Unassigned</span>}
                  <span className={'pill ' + (selectedCustomer.customer_type === 'cash' ? 'pill--cash' : 'pill--credit')}>
                    {selectedCustomer.customer_type === 'cash' ? 'Cash' : `Credit · ${selectedCustomer.credit_days || '—'}d`}
                  </span>
                  {selectedCustomer.flagged && <span className="pill pill--flagged">Flagged</span>}
                </div>
              </div>
            </div>

            {custDetailLoading ? (
              <div className="state-msg">Loading...</div>
            ) : custDetail ? (
              <>
                {/* Outstanding summary */}
                {cdActiveBills.length > 0 && (
                  <div className="card" style={{ marginBottom: 16 }}>
                    <div className="card-head">
                      <span className="chip chip--red" />
                      <span className="card-label">Outstanding</span>
                    </div>
                    <span className="sales-figure">{formatINR(cdTotalPending)}</span>
                    <p className="hero-sub" style={{ marginTop: 8 }}>
                      <strong>{cdActiveBills.length}</strong> {cdActiveBills.length === 1 ? 'bill' : 'bills'}
                      {cdOldestBill && (
                        <> · oldest {new Date(cdOldestBill.invoice_date + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' })}</>
                      )}
                    </p>
                  </div>
                )}

                {/* Insights — 3 stat cards */}
                <div className="three-col">
                  <div className="card" style={{ marginBottom: 0 }}>
                    <div className="stat-label">Total purchased (FY)</div>
                    <div className="stat-value">{cdFyTotal > 0 ? formatCompact(cdFyTotal) : '—'}</div>
                    <div className="stat-sub">{cdHistory.length} {cdHistory.length === 1 ? 'invoice' : 'invoices'}</div>
                  </div>
                  <div className="card" style={{ marginBottom: 0 }}>
                    <div className="stat-label">Avg order value</div>
                    <div className="stat-value">{cdAvgOrder > 0 ? formatCompact(Math.round(cdAvgOrder)) : '—'}</div>
                    <div className="stat-sub">per invoice this FY</div>
                  </div>
                  <div className="card" style={{ marginBottom: 0 }}>
                    <div className="stat-label">Last purchase</div>
                    <div className="stat-value">{cdDaysSince !== null ? `${cdDaysSince}d ago` : '—'}</div>
                    <div className="stat-sub">
                      {cdLastPurchase
                        ? new Date(cdLastPurchase + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })
                        : 'No purchases this FY'}
                    </div>
                  </div>
                </div>

                {/* Monthly purchase chart */}
                {cdHistory.length > 0 && (
                  <div className="card" style={{ marginBottom: 16 }}>
                    <div className="card-head">
                      <span className="chip chip--teal" />
                      <span className="card-label">Monthly purchases (this FY)</span>
                    </div>
                    <ResponsiveContainer width="100%" height={120}>
                      <BarChart data={cdChartData} margin={{ top: 4, right: 0, left: 0, bottom: 0 }}>
                        <CartesianGrid vertical={false} stroke="#E7E4DC" />
                        <XAxis
                          dataKey="date"
                          tick={{ fill: '#6B6A64', fontSize: 11, fontFamily: 'Manrope, sans-serif' }}
                          axisLine={{ stroke: '#E7E4DC' }}
                          tickLine={false}
                          interval={0}
                        />
                        <YAxis hide />
                        <Tooltip
                          cursor={{ fill: 'rgba(28,122,92,0.06)' }}
                          formatter={v => [formatINR(v), 'Purchases']}
                          contentStyle={{ background: '#fff', border: '1px solid #E7E4DC', borderRadius: 8, fontFamily: 'Manrope, sans-serif', fontSize: 13 }}
                        />
                        <Bar dataKey="total" fill="#1C7A5C" radius={[3, 3, 0, 0]} maxBarSize={48} />
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                )}

                {/* Rating */}
                {cdRating && (
                  <div className="card" style={{ marginBottom: 16 }}>
                    <div className="card-head">
                      <span className="chip chip--indigo" />
                      <span className="card-label">Customer rating</span>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
                      <div className="rating-stars">
                        {[1, 2, 3, 4, 5].map(i => (
                          <span key={i} className={'rating-star' + (i <= cdRating.stars ? ' rating-star--on' : '')} />
                        ))}
                      </div>
                      <div style={{ fontSize: 13, color: 'var(--text-secondary)' }}>{cdRating.reason}</div>
                    </div>
                  </div>
                )}

                {/* Purchase history table */}
                {cdRecentHistory.length > 0 && (
                  <div className="card">
                    <div className="card-head">
                      <span className="chip chip--indigo" />
                      <span className="card-label">
                        Purchase history — last {cdRecentHistory.length} invoices this FY
                      </span>
                    </div>
                    <table className="cust-table">
                      <thead>
                        <tr>
                          <th>Date</th>
                          <th>Invoice ref</th>
                          <th className="r">Amount</th>
                        </tr>
                      </thead>
                      <tbody>
                        {cdRecentHistory.map((h, i) => (
                          <tr key={i}>
                            <td>{new Date(h.sale_date + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' })}</td>
                            <td style={{ fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--text-secondary)' }}>{h.voucher_number || '—'}</td>
                            <td className="r">{h.amount > 0 ? formatINR(h.amount) : '—'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}

                {cdHistory.length === 0 && cdActiveBills.length === 0 && (
                  <p className="hero-sub" style={{ paddingTop: 8 }}>
                    No purchase history or outstanding bills on record for this FY.
                  </p>
                )}
              </>
            ) : null}
          </div>
        )}

      </div>
    </div>
  )
}
