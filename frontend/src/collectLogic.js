// Pure logic for the Collect tab — no React, no Supabase — split out from
// CollectTab.jsx so it can be unit-tested with plain Node (JSX can't be).

export const _istNow  = () => new Date(Date.now() + 330 * 60 * 1000)
export const _fmtDate = d =>
  `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')}`
export const TODAY = _fmtDate(_istNow())
export function _addDaysISO(dateStr, n) {
  const [y, m, d] = dateStr.split('-').map(Number)
  const dt = new Date(Date.UTC(y, m - 1, d))
  dt.setUTCDate(dt.getUTCDate() + n)
  return _fmtDate(dt)
}

// Priority tiers keyed on DAYS PAST DUE (days_overdue, relative to each
// bill's own due_date) — not the "Age of dues" card's buckets, which key off
// days since the bill date instead. Per-bill weight, then blended by amount
// so a customer's score reflects the real mix of how overdue their bills are.
export const OVERDUE_TIERS = [
  { max: 30,        weight: 1 },
  { max: 60,        weight: 1.5 },
  { max: 90,        weight: 2 },
  { max: 180,       weight: 2.5 },
  { max: Infinity,  weight: 3 },
]
export function overdueWeight(days) {
  for (const t of OVERDUE_TIERS) if (days <= t.max) return t.weight
  return 3
}

// Builds the three ranked sections from raw `outstanding` rows + a
// customer-lookup Map (id -> customer_list_view row). Excludes flagged
// customers, cash customers, stale bills, and any bill whose customer_id
// doesn't resolve to a known customer.
export function buildSections(rows, customersById, today = TODAY) {
  const byCustomer = new Map()
  for (const r of rows) {
    const cust = customersById.get(r.customer_id)
    if (!cust) continue
    if (cust.flagged) continue
    if (cust.customer_type === 'cash') continue
    if (r.age_status !== 'recent') {
      // Stale bills are excluded from every section, but a stale ON-ACCOUNT
      // CREDIT (negative pending_amount) still legitimately reduces what a
      // customer currently owes — an old unallocated payment doesn't expire
      // — so it's kept only for the netting step below, not for display.
      if (r.pending_amount < 0) {
        let g = byCustomer.get(r.customer_id)
        if (!g) { g = { cust, overdue: [], dueWeek: [], comingUp: [], credits: [] }; byCustomer.set(r.customer_id, g) }
        g.credits.push(r)
      }
      continue
    }
    let g = byCustomer.get(r.customer_id)
    if (!g) { g = { cust, overdue: [], dueWeek: [], comingUp: [], credits: [] }; byCustomer.set(r.customer_id, g) }
    if (r.pending_amount < 0) {
      g.credits.push(r)
      continue
    }
    if (r.pending_amount <= 0 || !r.due_date) continue
    if (r.due_date < today) g.overdue.push(r)
    else if (r.due_date <= _addDaysISO(today, 7)) g.dueWeek.push(r)
    else if (r.due_date <= _addDaysISO(today, 30)) g.comingUp.push(r)
  }

  const immediate = []
  const dueThisWeek = []
  const comingUp = []

  for (const g of byCustomer.values()) {
    if (g.overdue.length > 0) {
      const totalOverdue = g.overdue.reduce((s, b) => s + b.pending_amount, 0)
      const totalCredit  = g.credits.reduce((s, b) => s + b.pending_amount, 0) // already <= 0
      const netOverdue   = totalOverdue + totalCredit
      if (netOverdue > 0) {
        const weightedGross = g.overdue.reduce((s, b) => s + b.pending_amount * overdueWeight(b.days_overdue || 0), 0)
        const avgWeight = weightedGross / totalOverdue
        const worstDaysOverdue = Math.max(...g.overdue.map(b => b.days_overdue || 0))
        immediate.push({
          customer: g.cust,
          amount: netOverdue,
          grossAmount: totalOverdue,
          creditApplied: -totalCredit,
          daysOverdue: worstDaysOverdue,
          billCount: g.overdue.length,
          priorityScore: netOverdue * avgWeight,
        })
      }
    }
    if (g.dueWeek.length > 0) {
      dueThisWeek.push({
        customer: g.cust,
        amount: g.dueWeek.reduce((s, b) => s + b.pending_amount, 0),
        earliestDue: g.dueWeek.reduce((min, b) => (!min || b.due_date < min) ? b.due_date : min, null),
        billCount: g.dueWeek.length,
      })
    }
    if (g.comingUp.length > 0) {
      comingUp.push({
        customer: g.cust,
        amount: g.comingUp.reduce((s, b) => s + b.pending_amount, 0),
        earliestDue: g.comingUp.reduce((min, b) => (!min || b.due_date < min) ? b.due_date : min, null),
        billCount: g.comingUp.length,
      })
    }
  }

  immediate.sort((a, b) => b.priorityScore - a.priorityScore)
  dueThisWeek.sort((a, b) => a.earliestDue.localeCompare(b.earliestDue))
  comingUp.sort((a, b) => a.earliestDue.localeCompare(b.earliestDue))

  return { immediate, dueThisWeek, comingUp }
}
