// ETF Trading Dashboard - JavaScript
// Handles real-time updates and user interactions

// Global state
let refreshInterval = null;
let syncInterval = null;
let transactionInterval = null;
let autoBuyInterval = null;
let botRunning = false;
let autoBuyEnabled = false;
let currentProfitTarget = null;
let activeWrThreshold = -80;  // Updated from /api/config; default matches Config.WILLIAMS_R_THRESHOLD
let portfolioData = null;

// ── Hibernate / Wake-up Recovery ─────────────────────────────────────────────
// Handles Ulaa (aggressive Page Lifecycle freeze) AND Chrome after hibernate.
// Three complementary signals are used so at least one always fires:
//   1. visibilitychange  — fires when the OS un-hides the window
//   2. window online     — fires when network reconnects after sleep
//   3. pageshow          — fires on bfcache restore (Ulaa/Chrome back-forward)
//   4. Page Lifecycle "resume" — fires when browser unfreezes a frozen page
//   5. Monotonic-clock drift poll — fallback for browsers that suppress all events
//
// On any wake signal, _wakeRecovery() debounces itself (5 s) then:
//   a) restarts all setInterval timers (startAutoRefresh)
//   b) immediately fetches fresh data (loadStatus, loadPortfolio, loadMarketData)
//   c) calls the backend /api/wake endpoint so Python-side can resync too

let _lastHeartbeatMono = typeof performance !== 'undefined' ? performance.now() : Date.now();
let _wakeRecoveryTimer  = null;
const _WAKE_DEBOUNCE_MS = 5000;   // coalesce multiple events into one recovery
const _CLOCK_GAP_MS     = 8000;   // drift gap that signals suspend (8 s is safe; normal drift <200 ms)
const _HEARTBEAT_MS     = 10000;  // how often the drift-poll runs

function _wakeRecovery(source) {
    // Debounce — one recovery per wake event cluster
    if (_wakeRecoveryTimer) return;
    _wakeRecoveryTimer = setTimeout(() => {
        _wakeRecoveryTimer = null;
        console.warn(`[Wake] Recovery triggered by: ${source}`);

        // 1. Restart all polling intervals (they may have been killed by the browser)
        if (typeof startAutoRefresh === 'function') startAutoRefresh();

        // 2. Immediately fetch fresh data instead of waiting for next interval tick
        const safeFetch = fn => { try { if (typeof fn === 'function') fn(); } catch(e) {} };
        safeFetch(loadStatus);
        safeFetch(loadIndices);
        safeFetch(loadPortfolio);
        safeFetch(loadPositions);
        safeFetch(loadMarketData);
        safeFetch(loadBnHStatus);

        // 3. Tell the backend to resync portfolio + recheck WS health
        fetch('/api/wake', { method: 'POST' })
            .catch(() => { /* non-fatal — backend watchdog will handle it anyway */ });

        console.log('[Wake] Recovery complete — intervals restarted, data refreshed');
    }, _WAKE_DEBOUNCE_MS);
}

// Signal 1: visibilitychange (most reliable cross-browser)
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') _wakeRecovery('visibilitychange');
});

// Signal 2: network comes back online (fires after hibernate when NIC re-initialises)
window.addEventListener('online', () => _wakeRecovery('online'));

// Signal 3: pageshow (covers bfcache restores — Ulaa uses this heavily)
window.addEventListener('pageshow', (e) => {
    if (e.persisted) _wakeRecovery('pageshow-persisted');
});

// Signal 4: Page Lifecycle API "resume" event (Chromium 68+, Ulaa supports it)
document.addEventListener('resume', () => _wakeRecovery('page-lifecycle-resume'));

// Signal 5: Monotonic clock drift poll — fallback for browsers that suppress
// all above events while frozen. performance.now() pauses during freeze while
// Date.now() advances, so a large gap = the page was frozen/suspended.
(function _driftPoll() {
    const now = typeof performance !== 'undefined' ? performance.now() : Date.now();
    const wall = Date.now();

    // We track monotonic time between polls; a gap much larger than _HEARTBEAT_MS
    // means the browser froze us (hibernate / aggressive tab throttle)
    const elapsed = now - _lastHeartbeatMono;
    if (elapsed > _HEARTBEAT_MS + _CLOCK_GAP_MS) {
        console.warn(`[Wake] Drift detected: ${Math.round(elapsed)}ms since last heartbeat`);
        _wakeRecovery('clock-drift');
    }
    _lastHeartbeatMono = now;
    setTimeout(_driftPoll, _HEARTBEAT_MS);
})();
// ─────────────────────────────────────────────────────────────────────────────

// Active values (what the bot is currently using from settings.json)
let activeProfitTarget = null;
let activeMaxQty = null;
// Saved defaults (from settings.json default_* fields — server is the source of truth)
let savedDefaultProfit = null;
let savedDefaultMaxQty = null;
// Whether user has an active temp override in this session
let hasActiveOverride = false;

// Smart balance tracking - account for recent purchases not yet reflected in Zerodha API
const ZERODHA_SYNC_DELAY = 5 * 60 * 1000; // 5 minutes - time for Zerodha to sync balance

function getRecentPurchases() {
    const purchases = JSON.parse(localStorage.getItem('recentLiquidcasePurchases') || '[]');
    const now = Date.now();
    
    // Filter out purchases older than 5 minutes (Zerodha would have synced by then)
    const recentPurchases = purchases.filter(p => (now - p.timestamp) < ZERODHA_SYNC_DELAY);
    
    // Clean up old purchases from storage
    if (recentPurchases.length !== purchases.length) {
        localStorage.setItem('recentLiquidcasePurchases', JSON.stringify(recentPurchases));
    }
    
    return recentPurchases;
}

function addRecentPurchase(amount, quantity) {
    const purchases = getRecentPurchases();
    purchases.push({
        amount: amount,
        quantity: quantity,
        timestamp: Date.now()
    });
    localStorage.setItem('recentLiquidcasePurchases', JSON.stringify(purchases));
}

function getTotalRecentPurchases() {
    const purchases = getRecentPurchases();
    return purchases.reduce((total, p) => total + p.amount, 0);
}

// Initialize dashboard on page load
document.addEventListener('DOMContentLoaded', function() {
    console.log('Dashboard initialized');
    
    // Load initial data - CRITICAL: Load portfolio FIRST before market data
    // This ensures calculateBuyQuantity() has portfolio data available
    loadStatus();
    loadConfig();
    
    // Load portfolio first, then market data (so buy qty calculation works)
    loadPortfolio().then(() => {
        // Portfolio loaded - now load market data with correct buy quantities
        loadMarketData();
    }).catch(err => {
        console.error('Failed to load portfolio, loading market data anyway:', err);
        loadMarketData(); // Load anyway to show something
    });
    
    loadSignals();
    loadLogs();
    loadIndices();
    loadTransactions();
    
    // Start auto-refresh
    startAutoRefresh();
    
    // Update time
    updateTime();
    setInterval(updateTime, 1000);

    // Auto-start Active Strategy bot if not already running
    setTimeout(() => {
        fetch('/api/bot/status')
            .then(r => r.json())
            .then(d => {
                if (!d.running) {
                    console.log('Auto-starting Active Strategy bot...');
                    startBot();
                } else {
                    console.log('Active Strategy bot already running.');
                }
            })
            .catch(() => {
                // Status check failed — try starting anyway
                startBot();
            });
    }, 1500);

    // Auto-start Dip Accumulator silently on page load (no error toast)
    setTimeout(() => {
        fetch('/api/intraday/status')
            .then(r => r.json())
            .then(d => {
                if (d.running) {
                    updateBnHUI(true);
                    startBnHRefresh();
                } else if (d.available) {
                    fetch('/api/intraday/start', { method: 'POST' })
                        .then(r => r.json())
                        .then(sd => {
                            if (sd.success || sd.running) { updateBnHUI(true); }
                            startBnHRefresh();
                        })
                        .catch(() => startBnHRefresh());
                }
            })
            .catch(() => startBnHRefresh());
    }, 3000);
});

// Update current time with live indicator
function updateTime() {
    const now = new Date();
    const timeStr = now.toLocaleTimeString('en-IN', { 
        hour: '2-digit', 
        minute: '2-digit', 
        second: '2-digit' 
    });
    const updateEl = document.getElementById('update-time');
    updateEl.textContent = `🟢 LIVE ${timeStr}`;
    updateEl.style.color = '#10b981'; // Green for live
}

// Load system status
function loadStatus() {
    fetch('/api/status')
        .then(response => response.json())
        .then(data => {
            const modeBadge = document.getElementById('mode-badge');
            const botStatusBadge = document.getElementById('bot-status-badge');
            
            // Show mode with indicator
            if (data.mode === 'DRY_RUN') {
                modeBadge.textContent = '🟡 DRY RUN';
                modeBadge.style.backgroundColor = '#f59e0b';
            } else {
                modeBadge.textContent = '🔴 LIVE TRADING';
                modeBadge.style.backgroundColor = '#ef4444';
            }
            
            // Show bot status
            if (data.bot_running) {
                botStatusBadge.textContent = '▶ RUNNING';
                botStatusBadge.style.backgroundColor = '#10b981';
                botStatusBadge.style.color = 'white';
            } else {
                botStatusBadge.textContent = '⏸ PAUSED';
                botStatusBadge.style.backgroundColor = '#6b7280';
                botStatusBadge.style.color = 'white';
            }
            
            // Fade in both badges after setting text
            modeBadge.style.opacity = '1';
            modeBadge.style.transition = 'opacity 0.3s';
            botStatusBadge.style.opacity = '1';
            botStatusBadge.style.transition = 'opacity 0.3s';
            
            botRunning = data.bot_running;
            updateBotButtons();
        })
        .catch(error => console.error('Error loading status:', error));
}

// Load configuration
function loadConfig() {
    fetch('/api/config')
        .then(response => response.json())
        .then(data => {
            const modeSelect = document.getElementById('mode-select');
            if (modeSelect) {
                modeSelect.value = data.mode === 'DRY_RUN' ? 'dry' : 'live';
            }
            
            const profitValue = parseFloat(data.profit_target_pct);
            const maxQtyValue = data.test_quantity || 0;
            const slotsValue = data.slots_count || 2;
            
            // Server-sourced defaults (single source of truth)
            savedDefaultProfit = parseFloat(data.default_profit_target_pct || profitValue);
            savedDefaultMaxQty = parseInt(data.default_test_quantity || 0);
            
            // Current active values
            activeProfitTarget = profitValue;
            activeMaxQty = maxQtyValue;
            currentProfitTarget = profitValue;
            
            // Detect if there's an active override
            hasActiveOverride = (profitValue !== savedDefaultProfit) || (maxQtyValue !== savedDefaultMaxQty);
            
            // Sidebar: always show saved defaults
            const botProfitTarget = document.getElementById('bot-profit-target');
            const botMaxQty = document.getElementById('bot-max-qty');
            const botSlots = document.getElementById('bot-slots');
            const botMaxCashPerStock = document.getElementById('bot-max-cash-per-stock');
            const botMaxCashPerTx    = document.getElementById('bot-max-cash-per-tx');
            const botMinPriceDrop    = document.getElementById('bot-min-price-drop');
            const botBuyTime         = document.getElementById('bot-buy-time');
            if (botProfitTarget) botProfitTarget.value = savedDefaultProfit.toFixed(2);
            if (botMaxQty) botMaxQty.value = savedDefaultMaxQty;
            if (botSlots) botSlots.value = slotsValue;
            if (botMaxCashPerStock) botMaxCashPerStock.value = data.max_cash_per_stock ?? 0;
            if (botMaxCashPerTx)    botMaxCashPerTx.value    = data.max_cash_per_transaction ?? 0;
            if (botMinPriceDrop)    botMinPriceDrop.value    = data.min_price_drop_pct ?? 1.0;
            const botCashReserve = document.getElementById('bot-cash-reserve');
            if (botCashReserve) botCashReserve.value = data.cash_reserve ?? 5000;
            if (botBuyTime) {
                const t = data.buy_execution_time || '15:15';
                const anytimeToggle = document.getElementById('bot-anytime-toggle');
                if (t === 'anytime') {
                    if (anytimeToggle) anytimeToggle.checked = true;
                    botBuyTime.disabled = true;
                    botBuyTime.value = '15:15'; // keep dropdown on last real time
                } else {
                    if (anytimeToggle) anytimeToggle.checked = false;
                    botBuyTime.disabled = false;
                    botBuyTime.value = t;
                    if (!botBuyTime.value.match(/^\d{2}:\d{2}$/)) botBuyTime.value = '15:15';
                }
            }
            // Order type
            const savedOrderType = data.default_order_type || 'MARKET';
            const otInput = document.getElementById('bot-default-order-type');
            if (otInput) otInput.value = savedOrderType;
            setDefaultOrderType(savedOrderType, false); // update toggle UI without saving

            // BnH settings loaded via loadBnHStatus when tab is opened
            
            // Market Monitor: always reflect current server state
            const profitSelect = document.getElementById('profit-select');
            const testQuantityInput = document.getElementById('test-quantity');
            if (profitSelect) profitSelect.value = profitValue.toFixed(2);
            if (testQuantityInput) testQuantityInput.value = maxQtyValue;
            
            // Sync W%R threshold for action badge and legend
            activeWrThreshold = data.williams_r_threshold || -80;
            
            const legendProfitTarget = document.getElementById('legend-profit-target');
            if (legendProfitTarget) legendProfitTarget.textContent = profitValue.toFixed(0);
            
            const legendWrThreshold = document.getElementById('legend-wr-threshold');
            if (legendWrThreshold) legendWrThreshold.textContent = activeWrThreshold;
            
            const monitoringInfo = document.getElementById('monitoring-info');
            if (monitoringInfo) {
                monitoringInfo.textContent = `📋 Monitoring: ${data.active_etfs.join(', ')} + LIQUIDCASE`;
            }
        })
        .catch(error => console.error('Error loading config:', error));
}

// Load portfolio data
function loadPortfolio() {
    return fetch('/api/portfolio')
        .then(response => response.json())
        .then(data => {
            // Store globally for calculations
            portfolioData = data;
            
            // Update total value (element removed from portfolio tab but kept for compat)
            const totalValEl = document.getElementById('total-value');
            if (totalValEl) totalValEl.textContent = 
                `₹${formatNumber(data.total_value)}`;
            
            // Update today's P&L — explicit sign on both ₹ and %
            const todayPnlEl = document.getElementById('today-pnl');
            if (todayPnlEl) {
                const pnlSign = data.today_pnl >= 0 ? '+' : '-';
                const pnlPctStr = `${data.today_pnl_pct >= 0 ? '+' : ''}${data.today_pnl_pct.toFixed(2)}%`;
                todayPnlEl.textContent = `${pnlSign}₹${formatNumber(Math.abs(data.today_pnl))} (${pnlPctStr})`;
                todayPnlEl.style.color = data.today_pnl >= 0 ? 'var(--success)' : 'var(--danger)';
                const pnlRow = todayPnlEl.closest('.portfolio-stat-pnl-row');
                if (pnlRow) {
                    pnlRow.style.backgroundColor = data.today_pnl >= 0
                        ? 'rgba(16,185,129,0.06)' : 'rgba(239,68,68,0.06)';
                }
            }

            // Update LIQUIDCASE card value directly from portfolio data (qty × LTP)
            if (data.liquidcase) {
                const lcEl = document.getElementById('liquidcase-avail-cash');
                if (lcEl && data.liquidcase.value != null) {
                    lcEl.textContent = '₹' + data.liquidcase.value.toLocaleString('en-IN',
                        {minimumFractionDigits: 2, maximumFractionDigits: 2});
                    lcEl.title = `${data.liquidcase.quantity || 0} units × ₹${(data.liquidcase.price || 0).toFixed(2)}`;
                }
            }
            // Refresh Available Cash card from Kite funds API
            refreshFundsCards();
            
            // Update sidebar monitoring status (elements may not exist if card removed)
            if (data.slots && data.slots.active_etfs) {
                const monEl = document.getElementById('monitoring-status');
                if (monEl) monEl.textContent = `Monitoring: ${data.slots.active_etfs.join(', ')}`;

                const used  = data.slots.used;
                const total = data.slots.total;
                let statusText = used === 0
                    ? `${total} slots free`
                    : used === total
                        ? `${used} of ${total} held`
                        : `${used} of ${total} held, ${total - used} free`;
                const slotsEl = document.getElementById('slots-status-sidebar');
                if (slotsEl) slotsEl.textContent = statusText;
            }
            
            // Update holdings table
            updateHoldingsTable(data.holdings);
            
            // Log successful load for debugging
            console.log('✅ Portfolio data loaded successfully:', {
                totalValue: data.total_value,
                liquidcase: data.liquidcase.quantity,
                slots: data.slots
            });
        })
        .catch(error => console.error('Error loading portfolio:', error));
}

// Update holdings table and modal — incremental DOM update (no flicker, preserves scroll)
function updateHoldingsTable(holdings) {
    // Update sidebar badge count
    const badge = document.getElementById('holdings-count-badge');
    if (badge) {
        badge.textContent = holdings && holdings.length > 0 ? holdings.length : '0';
    }

    const modalBody = document.getElementById('holdings-modal-body');
    if (!modalBody) return;

    const activeEtfs = portfolioData?.slots?.active_etfs || [];
    const activeProfitTarget = currentProfitTarget;  // for Active strategy rows
    // bnhHarvestPct is the global from loadBnHStatus

    if (!holdings || holdings.length === 0) {
        modalBody.innerHTML = '<tr><td colspan="9" class="empty-state">No holdings data yet</td></tr>';
        return;
    }

    // Sort by P&L% descending so best performers appear first
    const sorted = [...holdings].sort((a, b) => b.pnl_pct - a.pnl_pct);

    // Append LIQUIDCASE as a cash-parking summary row at the bottom
    const lc = portfolioData?.liquidcase;
    if (lc && lc.quantity > 0) {
        sorted.push({
            symbol: 'LIQUIDCASE',
            quantity: lc.quantity,
            average_price: lc.price,
            ltp: lc.price,
            value: lc.value,
            pnl: 0,
            pnl_pct: 0,
            isLiquidcase: true
        });
    }

    // Build map of existing rows by symbol for incremental update
    const existingRows = new Map();
    for (const row of modalBody.querySelectorAll('tr[data-symbol]')) {
        existingRows.set(row.dataset.symbol, row);
    }

    const symbolsInData = new Set(sorted.map(h => h.symbol));

    // Remove rows for symbols no longer held
    for (const [sym, row] of existingRows) {
        if (!symbolsInData.has(sym)) row.remove();
    }

    // Remove empty-state row if it exists
    const emptyRow = modalBody.querySelector('td.empty-state');
    if (emptyRow) emptyRow.closest('tr').remove();

    // Update or insert rows in sorted order
    sorted.forEach((h, idx) => {
        const isMonitored = activeEtfs.includes(h.symbol);
        // Per-row target: Dip Acc. uses Harvest Target %, Active uses Profit Target %
        const rowTargetPct = (!h.isLiquidcase && h.strategy === 'bnh')
            ? (bnhHarvestPct || 5)
            : (activeProfitTarget || currentProfitTarget || 3);
        const targetPrice = h.average_price * (1 + rowTargetPct / 100);
        const targetReached = !h.isLiquidcase && h.pnl_pct >= rowTargetPct;
        const pnlClass = h.pnl_pct >= 0 ? 'pnl-positive' : 'pnl-negative';
        const pnlRupee = `${h.pnl >= 0 ? '+' : '-'}₹${formatNumber(Math.abs(h.pnl))}`;
        const pnlPct   = `${h.pnl_pct >= 0 ? '+' : ''}${h.pnl_pct.toFixed(2)}%`;

        let symbolBadge = '';
        if (h.isLiquidcase) {
            symbolBadge = '<span class="holding-badge holding-badge-cash">💵 CASH</span>';
        } else if (isMonitored) {
            symbolBadge = '<span class="holding-badge holding-badge-bot">🤖 BOT</span>';
        }

        // Strategy tag for col 1
        let strategyTag = '—';
        if (h.isLiquidcase) {
            strategyTag = '<span class="holding-strat-tag strat-cash">Cash</span>';
        } else if (h.strategy === 'bnh') {
            strategyTag = '<span class="holding-strat-tag strat-bnh">Dip Acc.</span>';
        } else if (h.strategy === 'active' || isMonitored) {
            strategyTag = '<span class="holding-strat-tag strat-active">Active</span>';
        }

        // Get or create the row
        let row = existingRows.get(h.symbol);
        if (!row) {
            row = document.createElement('tr');
            row.dataset.symbol = h.symbol;
            // Pre-fill 9 empty cells
            for (let i = 0; i < 9; i++) row.insertCell();
            if (h.isLiquidcase) row.classList.add('holding-liquidcase-row');
        }

        // Re-order: ensure this row is at the right position
        const orderedRows = Array.from(modalBody.querySelectorAll('tr[data-symbol]'));
        const currentPos = orderedRows.indexOf(row);
        if (currentPos !== idx) {
            const ref = orderedRows[idx];
            if (ref && ref !== row) {
                modalBody.insertBefore(row, ref);
            } else if (!ref) {
                modalBody.appendChild(row);
            }
        } else if (!row.parentElement) {
            modalBody.appendChild(row);
        }

        // Update cell content only when it actually changes (avoids reflow)
        const cells = row.cells;
        const c0 = `<strong>${escapeHtml(h.symbol)}</strong>${symbolBadge}`;
        if (cells[0].innerHTML !== c0) cells[0].innerHTML = c0;

        if (cells[1].innerHTML !== strategyTag) cells[1].innerHTML = strategyTag;

        const c2 = h.quantity.toLocaleString('en-IN');
        if (cells[2].textContent !== c2) cells[2].textContent = c2;

        const c3 = `₹${h.average_price.toFixed(2)}`;
        if (cells[3].textContent !== c3) cells[3].textContent = c3;

        const c4 = `₹${h.ltp.toFixed(2)}`;
        if (cells[4].textContent !== c4) cells[4].textContent = c4;

        const c5 = `₹${formatNumber(h.value)}`;
        if (cells[5].textContent !== c5) cells[5].textContent = c5;

        if (h.isLiquidcase) {
            if (cells[6].textContent !== '—') { cells[6].textContent = '—'; cells[6].className = ''; }
            if (cells[7].textContent !== '—') { cells[7].textContent = '—'; cells[7].className = ''; }
            if (cells[8].textContent !== '—') { cells[8].textContent = '—'; cells[8].className = ''; }
        } else {
            if (cells[6].textContent !== pnlRupee) { cells[6].textContent = pnlRupee; cells[6].className = pnlClass; }
            if (cells[7].textContent !== pnlPct)   { cells[7].textContent = pnlPct;   cells[7].className = pnlClass; }

            const targetContent = targetReached
                ? `₹${targetPrice.toFixed(2)} (${rowTargetPct}%) ✓ HIT`
                : `₹${targetPrice.toFixed(2)} (${rowTargetPct}%)`;
            if (cells[8].innerHTML !== targetContent) {
                if (targetReached) {
                    cells[8].innerHTML = `₹${targetPrice.toFixed(2)} <span style="font-size:0.65rem;color:var(--text-muted)">(${rowTargetPct}%)</span> <span class="target-badge">✓ HIT</span>`;
                } else {
                    cells[8].innerHTML = `₹${targetPrice.toFixed(2)} <span style="font-size:0.65rem;color:var(--text-muted)">(${rowTargetPct}%)</span>`;
                }
                cells[8].className = targetReached ? 'target-reached' : '';
            }
        }
    });
}

// ── Current Positions (all Zerodha net positions) ─────────────────────────────
function loadPositions(forceRefresh = false) {
    const btn = document.querySelector('[onclick="loadPositions()"]');
    if (btn) { btn.textContent = '⟳'; btn.disabled = true; }

    // Pass ?refresh=1 on manual button clicks so the 10-second server cache is
    // bypassed for explicit user refreshes but respected for auto-polls.
    // If the button exists, this was a manual click → force refresh.
    const shouldForce = forceRefresh || !!btn;
    const url = shouldForce ? '/api/positions?refresh=1' : '/api/positions';
    fetch(url)
        .then(r => r.json())
        .then(d => {
            if (btn) { btn.textContent = '⟳ Refresh'; btn.disabled = false; }
            if (d.error) {
                const tbody = document.getElementById('positions-body');
                if (tbody) tbody.innerHTML = `<tr><td colspan="7" class="empty-state" style="color:var(--danger)">Error: ${d.error}</td></tr>`;
                return;
            }

            // Update live time
            const timeEl = document.getElementById('positions-live-time');
            if (timeEl) timeEl.textContent = new Date().toLocaleTimeString('en-IN', {hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false});

            // Summary bar
            const summaryEl = document.getElementById('positions-summary');
            if (summaryEl) {
                const pnlSign = d.total_pnl >= 0 ? '+' : '';
                summaryEl.textContent = `${d.count} open · P&L: ${pnlSign}₹${formatNumber(Math.abs(d.total_pnl))} · Day: ${d.total_day_pnl >= 0 ? '+' : ''}₹${formatNumber(Math.abs(d.total_day_pnl))}`;
                summaryEl.style.color = d.total_pnl >= 0 ? 'var(--success)' : 'var(--danger)';
            }

            const tbody = document.getElementById('positions-body');
            if (!tbody) return;

            if (!d.positions || !d.positions.length) {
                tbody.innerHTML = '<tr><td colspan="7" class="empty-state">No open positions</td></tr>';
                document.getElementById('positions-totals').style.display = 'none';
                return;
            }

            tbody.innerHTML = d.positions.map(p => {
                const pnlCls    = p.pnl >= 0    ? 'pnl-positive' : 'pnl-negative';
                const dayPnlCls = p.day_pnl >= 0 ? 'pnl-positive' : 'pnl-negative';
                const pnlSign   = p.pnl >= 0    ? '+' : '-';
                const daySign   = p.day_pnl >= 0 ? '+' : '-';
                const pnlPctStr = `${p.pnl_pct >= 0 ? '+' : ''}${p.pnl_pct.toFixed(2)}%`;
                const qtyStyle  = p.quantity > 0 ? 'color:var(--success);font-weight:700;' : 'color:var(--danger);font-weight:700;';

                return `<tr>
                    <td><strong>${escapeHtml(p.symbol)}</strong></td>
                    <td style="font-size:0.72rem">${p.product}</td>
                    <td style="${qtyStyle}">${p.quantity > 0 ? '+' : ''}${p.quantity.toLocaleString('en-IN')}</td>
                    <td>₹${p.avg_price.toFixed(2)}</td>
                    <td><strong>₹${p.ltp.toFixed(2)}</strong></td>
                    <td class="${pnlCls}">${pnlSign}₹${formatNumber(Math.abs(p.pnl))}</td>
                    <td class="${pnlCls}">${pnlPctStr}</td>
                </tr>`;
            }).join('');

            // Totals footer
            const totalsEl = document.getElementById('positions-totals');
            if (totalsEl) {
                totalsEl.style.display = 'flex';
                const tvEl  = document.getElementById('pos-total-value');
                const tpEl  = document.getElementById('pos-total-pnl');
                const dpEl  = document.getElementById('pos-day-pnl');
                if (tvEl) tvEl.textContent = `₹${formatNumber(d.total_value)}`;
                if (tpEl) { tpEl.textContent = `${d.total_pnl >= 0 ? '+' : ''}₹${formatNumber(Math.abs(d.total_pnl))}`; tpEl.style.color = d.total_pnl >= 0 ? 'var(--success)' : 'var(--danger)'; }
                if (dpEl) { dpEl.textContent = `${d.total_day_pnl >= 0 ? '+' : ''}₹${formatNumber(Math.abs(d.total_day_pnl))}`; dpEl.style.color = d.total_day_pnl >= 0 ? 'var(--success)' : 'var(--danger)'; }
            }
        })
        .catch(err => {
            if (btn) { btn.textContent = '⟳ Refresh'; btn.disabled = false; }
            const tbody = document.getElementById('positions-body');
            if (tbody) tbody.innerHTML = `<tr><td colspan="7" class="empty-state" style="color:var(--danger)">Failed to load positions</td></tr>`;
        });
}

// Load market data
function loadMarketData() {
    fetch('/api/market')
        .then(response => response.json())
        .then(data => {
            updateMarketTable(data);
        })
        .catch(error => console.error('Error loading market data:', error));
}

// Update market table
// Store manual trade quantities to prevent overwrite during refresh
const manualTradeQty = {};

// Cache for existing rows to avoid recreating DOM
const rowCache = new Map();

// Calculate recommended buy quantity based on slot value
function calculateBuyQuantity(etfPrice) {
    if (!etfPrice || etfPrice <= 0) {
        return activeMaxQty > 0 ? activeMaxQty : 1;
    }

    // Primary: use max_cash_per_transaction from Saved Defaults
    // Read from the Controls tab input so it always reflects the saved value
    const txInput = document.getElementById('bot-max-cash-per-tx');
    const maxCashPerTx = txInput ? parseFloat(txInput.value) || 0 : 0;

    if (maxCashPerTx > 0) {
        const qty = Math.floor(maxCashPerTx / etfPrice);
        const finalQty = activeMaxQty > 0 ? Math.min(qty, activeMaxQty) : qty;
        console.debug(`✓ Buy qty for ₹${etfPrice}: ${finalQty} units (budget ₹${maxCashPerTx}, maxQty cap: ${activeMaxQty || 'none'})`);
        return Math.max(1, finalQty);
    }

    // Fallback: slot-based division (if max_cash_per_transaction not set)
    if (portfolioData) {
        const slotsTotal    = portfolioData.slots?.total || 2;
        const liquidcaseQty = portfolioData.liquidcase?.quantity || 0;
        const liquidcasePrice = portfolioData.liquidcase?.price || 0;
        const liquidcaseValue = liquidcaseQty * liquidcasePrice;

        if (liquidcaseValue > 0) {
            const slotValue = liquidcaseValue / slotsTotal;
            const qty = Math.floor(slotValue / etfPrice);
            const finalQty = activeMaxQty > 0 ? Math.min(qty, activeMaxQty) : qty;
            console.debug(`✓ Buy qty (slot fallback) for ₹${etfPrice}: ${finalQty} units (slot ₹${slotValue.toFixed(0)})`);
            return Math.max(1, finalQty);
        }
    }

    return activeMaxQty > 0 ? activeMaxQty : 1;
}

// Update market table with INCREMENTAL updates (only changed cells)
// Cache last market data for order type lookups
let marketDataCache = [];
let bnhLatestCache  = null;   // latest BnH status payload for signal summary
let bnhHarvestPct   = 5;      // live bnh_partial_profit_pct from top-level status

function _sortedMarketData(marketData) {
    return [...marketData].sort((a, b) => {
        // Most negative W%R on top (most oversold first)
        const wa = a.williams_r != null ? a.williams_r : 1;  // null sorts to bottom
        const wb = b.williams_r != null ? b.williams_r : 1;
        return wa - wb;
    });
}

function updateMarketTable(marketData) {
    marketDataCache = marketData;
    updateSignalSummary();   // refresh Activity tab signal summary
    const tbody = document.getElementById('market-body');
    if (!tbody) return;

    if (!marketData || marketData.length === 0) {
        tbody.innerHTML = '<tr><td colspan="13" class="empty-state">No market data</td></tr>';
        rowCache.clear();
        return;
    }

    // Sort: BUY / SELL rows first, then HOLD, WAIT, WATCH
    const sorted = _sortedMarketData(marketData);

    // Check if table needs initialization
    const needsInit = tbody.children.length === 0 || tbody.querySelector('.empty-state') || rowCache.size === 0;
    
    if (needsInit) {
        // First time: build complete table in sorted order
        console.log('📊 Building complete market table...');
        buildCompleteTable(tbody, sorted);
        return;
    }
    
    // Incremental update: patch cell values + W%R row highlight
    sorted.forEach(m => {
        updateMarketRow(tbody, m);
        // Highlight entire row when W%R <= -80 (oversold threshold)
        const cached = rowCache.get(m.symbol);
        if (cached && cached.row) {
            const wr = m.williams_r != null ? m.williams_r : 0;
            cached.row.classList.toggle('wr-oversold-row', wr <= -80);
        }
    });

    // Re-order DOM rows to match current sort without rebuilding
    sorted.forEach(m => {
        const cached = rowCache.get(m.symbol);
        if (cached && cached.row) tbody.appendChild(cached.row);
    });

    // Auto-refresh top-bid into Limit ₹ inputs for all LIMIT-mode symbols
    _autoRefreshLimitPrices();
}

function _autoRefreshLimitPrices() {
    const now = Date.now();
    // Effective global order type from the Controls tab toggle
    const globalOT = (document.getElementById('bot-default-order-type')?.value || 'MARKET').toUpperCase();
    if (globalOT !== 'LIMIT') return;   // nothing to do when global mode is MARKET

    // Iterate every symbol currently rendered in the market table
    if (!marketDataCache) return;
    marketDataCache.forEach(m => {
        const symbol = m.symbol;
        // Effective order type: per-row override, else global
        const effOT = (manualOrderType[symbol] || globalOT).toUpperCase();
        if (effOT !== 'LIMIT') return;

        // The limit price input for this symbol
        const inp = document.getElementById(`lp-${symbol}`);
        if (!inp) return;                       // input not in DOM yet
        if (inp.dataset.userEdited) return;     // user typed a custom price — don't overwrite

        // Throttle: at most once per LIMIT_REFRESH_INTERVAL_MS per symbol
        const last = _limitRefreshTs[symbol] || 0;
        if (now - last < LIMIT_REFRESH_INTERVAL_MS) return;
        _limitRefreshTs[symbol] = now;

        fetch(`/api/depth/${symbol}`)
            .then(r => r.json())
            .then(d => {
                if (!d.top_bid) return;
                const fresh = Number(d.top_bid).toFixed(2);
                // Update market data cache
                const row = marketDataCache?.find(r => r.symbol === symbol);
                if (row) row.top_bid = d.top_bid;
                // Update input only if user hasn't typed a custom value
                const liveInp = document.getElementById(`lp-${symbol}`);
                if (liveInp && !liveInp.dataset.userEdited) {
                    liveInp.value = fresh;
                    manualLimitPrice[symbol] = fresh;
                }
                // Refresh depth strip bid label
                const ctrl = document.getElementById(`buy-ctrl-${symbol}`);
                if (ctrl) {
                    const bidEl = ctrl.querySelector('.depth-bid');
                    if (bidEl) bidEl.textContent = `B:₹${fresh}`;
                }
            })
            .catch(() => {});
    });
}

// Build complete table (first time only)
function buildCompleteTable(tbody, marketData) {
    try {
        const rows = marketData.map(m => createMarketRow(m));
        tbody.innerHTML = ''; // Clear
        rows.forEach(row => tbody.appendChild(row));
        
        // CRITICAL: Clear manual trade cache on table rebuild
        // This ensures inputs always show fresh server quantities
        Object.keys(manualTradeQty).forEach(key => delete manualTradeQty[key]);
        console.log('🔄 Manual trade cache cleared on table rebuild');
        
        // Cache all rows
        rowCache.clear(); // Clear old cache
        marketData.forEach(m => {
            const row = tbody.querySelector(`tr[data-symbol="${m.symbol}"]`);
            if (row) {
                rowCache.set(m.symbol, {
                    row: row,
                    cells: {
                        ltp: row.querySelector('.col-ltp'),
                        avg: row.querySelector('.col-avg'),
                        change: row.querySelector('.col-change'),
                        wr: row.querySelector('.col-wr'),
                        qty: row.querySelector('.col-qty'),
                        target: row.querySelector('.col-target'),
                        plAmt: row.querySelector('.col-pl-amt'),
                        pl: row.querySelector('.col-pl'),
                        action: row.querySelector('.col-action'),
                        manual: row.querySelector('.col-manual')
                    },
                    lastValues: {
                        qty: m.qty_held,  // Track numeric quantity
                        qtyDisplay: m.qty_held > 0 ? m.qty_held : '---',  // Track display text
                        isHeld: m.qty_held > 0  // Track holding status
                    }
                });
            }
        });
        console.log(`✅ Table built: ${rowCache.size} rows cached`);
    } catch (error) {
        console.error('❌ Error building table:', error);
        // Fallback to simple innerHTML
        tbody.innerHTML = marketData.map(m => `
            <tr data-symbol="${m.symbol}">
                <td><strong>${m.symbol}</strong></td>
                <td>₹${m.ltp ? m.ltp.toFixed(2) : '0.00'}</td>
                <td>${m.avg_price ? `₹${m.avg_price.toFixed(2)}` : '---'}</td>
                <td>${m.change_pct >= 0 ? '+' : ''}${m.change_pct.toFixed(2)}%</td>
                <td>${m.williams_r !== null ? m.williams_r.toFixed(1) : '---'}</td>
                <td>${m.qty_held > 0 ? m.qty_held : '---'}</td>
                <td>${m.avg_price ? `₹${m.avg_price.toFixed(2)}` : '---'}</td>
                <td>${m.target_price ? `₹${m.target_price.toFixed(2)}` : '---'}</td>
                <td>---</td>
                <td>---</td>
                <td>---</td>
                <td>---</td>
            </tr>
        `).join('');
    }
}

// Create a single row (DOM element, not HTML string)
function createMarketRow(m) {
    const tr = document.createElement('tr');
    tr.dataset.symbol = m.symbol;
    
    const changeClass = m.change_pct >= 0 ? 'pnl-positive' : 'pnl-negative';
    const changeText = `${m.change_pct >= 0 ? '+' : ''}${m.change_pct.toFixed(2)}%`;
    const avgText = m.avg_price ? `₹${m.avg_price.toFixed(2)}` : '---';
    const wrValue = m.williams_r !== null ? m.williams_r.toFixed(1) : '---';
    const wrClass = m.williams_r !== null && m.williams_r <= -80 ? 'wr-oversold' : '';
    const qtyText = m.qty_held > 0 ? m.qty_held : '---';
    const targetText = m.target_price ? `₹${m.target_price.toFixed(2)}` : '---';
    
    // P&L Amount
    let plAmtText = '---';
    let plAmtClass = '';
    if (m.profit_amount !== null && m.profit_amount !== undefined) {
        plAmtText = `${m.profit_amount >= 0 ? '+' : '-'}₹${Math.abs(m.profit_amount).toFixed(2)}`;
        plAmtClass = m.profit_amount >= 0 ? 'pnl-positive' : 'pnl-negative';
    }
    
    // P&L Percentage
    let plText = '---';
    let plClass = '';
    if (m.profit_pct !== null) {
        plText = `${m.profit_pct >= 0 ? '+' : ''}${m.profit_pct.toFixed(2)}%`;
        if (m.profit_pct >= currentProfitTarget) {
            plClass = 'pnl-target';
        } else if (m.profit_pct >= 0) {
            plClass = 'pnl-positive';
        } else {
            plClass = 'pnl-negative';
        }
    }
    
    tr.innerHTML = `
        <td class="col-symbol"><strong>${m.symbol}</strong></td>
        <td class="col-ltp">₹${m.ltp ? m.ltp.toFixed(2) : '0.00'}</td>
        <td class="col-avg">${avgText}</td>
        <td class="col-change ${changeClass}">${changeText}</td>
        <td class="col-wr ${wrClass}">${wrValue}</td>
        <td class="col-qty">${qtyText}</td>
        <td class="col-target">${targetText}</td>
        <td class="col-pl-amt ${plAmtClass}">${plAmtText}</td>
        <td class="col-pl ${plClass}">${plText}</td>
        <td class="col-action">${getActionBadgeHTML(m)}</td>
        <td class="col-manual">${getTradeControlsHTML(m)}</td>
    `;
    
    return tr;
}

// Update a single row (only changed cells)
function updateMarketRow(tbody, m) {
    let cached = rowCache.get(m.symbol);
    
    // If row doesn't exist, create it
    if (!cached) {
        const newRow = createMarketRow(m);
        tbody.appendChild(newRow);
        cached = {
            row: newRow,
            cells: {
                ltp: newRow.querySelector('.col-ltp'),
                avg: newRow.querySelector('.col-avg'),
                change: newRow.querySelector('.col-change'),
                wr: newRow.querySelector('.col-wr'),
                qty: newRow.querySelector('.col-qty'),
                target: newRow.querySelector('.col-target'),
                plAmt: newRow.querySelector('.col-pl-amt'),
                pl: newRow.querySelector('.col-pl'),
                action: newRow.querySelector('.col-action'),
                manual: newRow.querySelector('.col-manual')
            },
            lastValues: {
                qty: m.qty_held,  // Track numeric quantity for comparison
                qtyDisplay: m.qty_held > 0 ? m.qty_held : '---',  // Track display text
                isHeld: m.qty_held > 0  // Track holding status
            }
        };
        rowCache.set(m.symbol, cached);
    }
    
    const { cells, lastValues } = cached;
    
    // Update LTP (with flash animation)
    const newLtp = m.ltp ? `₹${m.ltp.toFixed(2)}` : '₹0.00';
    if (lastValues.ltp !== newLtp) {
        cells.ltp.textContent = newLtp;
        if (lastValues.ltp !== undefined) {
            flashCell(cells.ltp, m.ltp > parseFloat(lastValues.ltp.replace('₹', '')) ? 'green' : 'red');
        }
        lastValues.ltp = newLtp;
    }
    
    // Update Avg Price
    const newAvg = m.avg_price ? `₹${m.avg_price.toFixed(2)}` : '---';
    if (lastValues.avg !== newAvg) {
        cells.avg.textContent = newAvg;
        lastValues.avg = newAvg;
    }
    
    // Update Change %
    const changeText = `${m.change_pct >= 0 ? '+' : ''}${m.change_pct.toFixed(2)}%`;
    if (lastValues.change !== changeText) {
        cells.change.textContent = changeText;
        cells.change.className = `col-change ${m.change_pct >= 0 ? 'pnl-positive' : 'pnl-negative'}`;
        lastValues.change = changeText;
    }
    
    // Update Williams %R (with flash animation)
    const wrValue = m.williams_r !== null ? m.williams_r.toFixed(1) : '---';
    if (lastValues.wr !== wrValue) {
        cells.wr.textContent = wrValue;
        cells.wr.className = `col-wr ${m.williams_r !== null && m.williams_r <= -80 ? 'wr-oversold' : ''}`;
        if (lastValues.wr !== undefined && wrValue !== '---') {
            flashCell(cells.wr, 'blue');
        }
        lastValues.wr = wrValue;
    }
    
    // Update Quantity
    const qtyText = m.qty_held > 0 ? m.qty_held : '---';
    if (lastValues.qtyDisplay !== qtyText) {
        cells.qty.textContent = qtyText;
        lastValues.qtyDisplay = qtyText;
    }
    
    // Update Target Price
    const targetText = m.target_price ? `₹${m.target_price.toFixed(2)}` : '---';
    if (lastValues.target !== targetText) {
        cells.target.textContent = targetText;
        lastValues.target = targetText;
    }
    
    // Update P/L Amount
    let plAmtText = '---';
    let plAmtClass = '';
    if (m.profit_amount !== null && m.profit_amount !== undefined) {
        plAmtText = `${m.profit_amount >= 0 ? '+' : '-'}₹${Math.abs(m.profit_amount).toFixed(2)}`;
        plAmtClass = m.profit_amount >= 0 ? 'pnl-positive' : 'pnl-negative';
    }
    if (lastValues.plAmt !== plAmtText) {
        cells.plAmt.textContent = plAmtText;
        cells.plAmt.className = `col-pl-amt ${plAmtClass}`;
        lastValues.plAmt = plAmtText;
    }
    
    // Update P/L %
    let plText = '---';
    let plClass = '';
    if (m.profit_pct !== null) {
        plText = `${m.profit_pct >= 0 ? '+' : ''}${m.profit_pct.toFixed(2)}%`;
        if (m.profit_pct >= currentProfitTarget) {
            plClass = 'pnl-target';
        } else if (m.profit_pct >= 0) {
            plClass = 'pnl-positive';
        } else {
            plClass = 'pnl-negative';
        }
    }
    if (lastValues.pl !== plText) {
        cells.pl.textContent = plText;
        cells.pl.className = `col-pl ${plClass}`;
        lastValues.pl = plText;
    }
    
    // Update Action badge (only if changed)
    const actionHTML = getActionBadgeHTML(m);
    if (lastValues.action !== actionHTML) {
        cells.action.innerHTML = actionHTML;
        lastValues.action = actionHTML;
    }
    
    // Update bid/ask depth in-place (every refresh, no DOM rebuild)
    const newBid = m.top_bid ? `B:₹${Number(m.top_bid).toFixed(2)}` : 'B:—';
    const newAsk = m.top_ask ? `A:₹${Number(m.top_ask).toFixed(2)}` : 'A:—';
    if (lastValues.bid !== newBid) {
        const bidEl = document.getElementById(`bid-${m.symbol}`);
        if (bidEl) bidEl.textContent = newBid;
        lastValues.bid = newBid;
    }
    if (lastValues.ask !== newAsk) {
        const askEl = document.getElementById(`ask-${m.symbol}`);
        if (askEl) askEl.textContent = newAsk;
        lastValues.ask = newAsk;
    }

    // Update Manual controls (if holding status OR quantity changed)
    const isHeld = m.qty_held > 0;
    const qtyChanged = lastValues.qty !== m.qty_held;

    if (lastValues.isHeld !== isHeld || qtyChanged) {
        // CRITICAL: Clear ALL cached manual trade quantities for this symbol
        // This ensures input fields always show current server quantity
        delete manualTradeQty[`${m.symbol}_exit`];
        delete manualTradeQty[`${m.symbol}_buy`];
        
        console.log(`🔄 Manual trade cache cleared for ${m.symbol}: qty changed ${lastValues.qty} → ${m.qty_held}`);
        
        cells.manual.innerHTML = getTradeControlsHTML(m);
        
        // CRITICAL FIX: Force update input value from server after HTML rebuild
        // This ensures the input always shows the fresh server quantity, not cached value
        if (isHeld) {
            const input = cells.manual.querySelector('.exit-input');
            if (input) {
                input.value = m.qty_held;
                input.max = m.qty_held;
                console.log(`✅ Forced exit input to server quantity: ${m.qty_held}`);
            }
        } else {
            const recommendedQty = calculateBuyQuantity(m.ltp);
            const input = cells.manual.querySelector('.buy-input');
            if (input) {
                input.value = recommendedQty;
                input.placeholder = recommendedQty;
                console.log(`✅ Forced buy input to recommended quantity: ${recommendedQty}`);
            }
        }
        
        lastValues.isHeld = isHeld;
        lastValues.qty = m.qty_held;
    }
}

// Get action badge HTML
// For HELD positions: client-side SELL/HOLD check (uses real-time profit% for instant response)
// For NOT-HELD positions: use backend m.action (already accounts for available_slots and W%R threshold)
function getActionBadgeHTML(m) {
    const profitPct = m.profit_pct || 0;
    const williamsR = m.williams_r || 0;
    const profitTarget = currentProfitTarget || 5;
    
    if (m.is_held) {
        // Client-side SELL/HOLD — needs real-time profit% from live LTP
        if (profitPct >= profitTarget) {
            return `
                <div class="signal-badge signal-sell">
                    <span class="signal-icon">🔴</span>
                    <span class="signal-text">SELL</span>
                    <span class="signal-reason">+${profitPct.toFixed(2)}%</span>
                </div>
            `;
        } else {
            return `
                <span class="action-badge action-hold">
                    🔵 HOLD
                    <small style="display:block; font-size:0.7em; opacity:0.7; margin-top:2px;">
                        ${profitPct >= 0 ? '+' : ''}${profitPct.toFixed(2)}% / ${profitTarget.toFixed(2)}%
                    </small>
                </span>
            `;
        }
    } else {
        // Use backend action — it correctly checks available_slots and W%R threshold
        const action = m.action || 'WATCH';
        
        if (action === 'BUY') {
            return `
                <div class="signal-badge signal-buy">
                    <span class="signal-icon">🟢</span>
                    <span class="signal-text">BUY</span>
                    <span class="signal-reason">W%R: ${williamsR.toFixed(0)}</span>
                </div>
            `;
        } else if (action === 'WAIT') {
            // Oversold but no slots available
            return `
                <span class="action-badge action-hold" style="background:rgba(100,100,100,0.3)">
                    ⏸ WAIT
                    <small style="display:block; font-size:0.7em; opacity:0.7; margin-top:2px;">
                        W%R: ${williamsR.toFixed(0)} · No slots
                    </small>
                </span>
            `;
        } else {
            return `
                <span class="action-badge action-watch">
                    ⚪ WATCH
                    <small style="display:block; font-size:0.7em; opacity:0.7; margin-top:2px;">
                        W%R: ${williamsR.toFixed(0)} / ${activeWrThreshold}
                    </small>
                </span>
            `;
        }
    }
}

// Helper: render depth strip (shared by buy and exit rows)
function _depthStripHTML(m) {
    const bidLabel = m.top_bid ? `₹${Number(m.top_bid).toFixed(2)}` : '—';
    const askLabel = m.top_ask ? `₹${Number(m.top_ask).toFixed(2)}` : '—';
    return `<div class="depth-strip" id="ds-${m.symbol}">
        <span class="depth-bid" id="bid-${m.symbol}" title="Top bid">B:${bidLabel}</span>
        <span class="depth-sep">·</span>
        <span class="depth-ask" id="ask-${m.symbol}" title="Top ask">A:${askLabel}</span>
    </div>`;
}

// Get trade controls HTML
function getTradeControlsHTML(m) {
    if (m.qty_held > 0) {
        const exitQty   = manualTradeQty[`${m.symbol}_exit`] || m.qty_held;
        const defaultOT = document.getElementById('bot-default-order-type')?.value || 'MARKET';
        const savedOT   = manualOrderType[m.symbol] || defaultOT;
        const isLimit   = savedOT === 'LIMIT';
        const topBid    = m.top_bid ? m.top_bid.toFixed(2) : '';
        const savedLP   = manualLimitPrice[m.symbol] || topBid || (m.ltp ? m.ltp.toFixed(2) : '');
        return `
            <div class="trade-controls exit-controls" id="buy-ctrl-${m.symbol}">
                ${_depthStripHTML(m)}
                <div class="trade-group">
                    <input
                        type="number"
                        class="trade-qty-input exit-input"
                        data-symbol="${m.symbol}"
                        data-type="exit"
                        value="${exitQty}"
                        min="1"
                        max="${m.qty_held}"
                        title="Qty to exit (max: ${m.qty_held} held)"
                    />
                    <button
                        class="reset-qty-btn"
                        onclick="resetTradeQty('${m.symbol}', 'exit')"
                        title="Reset to full holding: ${m.qty_held}"
                    >↺</button>
                    <div class="order-type-mini">
                        <button class="ot-mini ${isLimit?'':'active'}"
                            onclick="setRowOrderType('${m.symbol}','MARKET',this)">MKT</button>
                        <button class="ot-mini ${isLimit?'active':''}"
                            onclick="setRowOrderType('${m.symbol}','LIMIT',this)">LMT</button>
                    </div>
                    <button
                        class="trade-btn exit-btn"
                        onclick="manualExit('${m.symbol}', ${m.qty_held})"
                    >🔴 Exit</button>
                </div>
                ${isLimit ? `
                <div class="limit-price-row" id="lp-row-${m.symbol}">
                    <span class="lp-label">Limit ₹</span>
                    <input
                        type="number"
                        class="trade-qty-input limit-price-input"
                        id="lp-${m.symbol}"
                        data-symbol="${m.symbol}"
                        value="${savedLP}"
                        step="0.05"
                        placeholder="Price"
                        oninput="manualLimitPrice['${m.symbol}']=this.value; this.dataset.userEdited=1"
                    />
                    <button class="lp-bid-btn" onclick="setLimitToTopBid('${m.symbol}')" title="Use top bid">= Bid</button>
                    <button class="lp-ask-btn" onclick="setLimitToTopAsk('${m.symbol}')" title="Use top ask">= Ask</button>
                </div>` : ''}
            </div>
        `;
    } else {
        const recommendedQty = calculateBuyQuantity(m.ltp);
        const buyQty    = manualTradeQty[`${m.symbol}_buy`] || recommendedQty;
        const topBid    = m.top_bid ? m.top_bid.toFixed(2) : '';
        const defaultOT = document.getElementById('bot-default-order-type')?.value || 'MARKET';
        const savedOT   = manualOrderType[m.symbol] || defaultOT;
        const isLimit   = savedOT === 'LIMIT';
        const savedLP   = manualLimitPrice[m.symbol] || topBid || (m.ltp ? m.ltp.toFixed(2) : '');
        return `
            <div class="trade-controls buy-controls" id="buy-ctrl-${m.symbol}">
                ${_depthStripHTML(m)}
                <div class="trade-group">
                    <input
                        type="number"
                        class="trade-qty-input buy-input"
                        data-symbol="${m.symbol}"
                        data-type="buy"
                        data-ltp="${m.ltp}"
                        value="${buyQty}"
                        min="1"
                        placeholder="${recommendedQty}"
                        title="Qty based on Max Cash/Tx ÷ LTP"
                    />
                    <button
                        class="reset-qty-btn"
                        onclick="resetTradeQty('${m.symbol}', 'buy')"
                        title="Reset qty"
                    >↺</button>
                    <div class="order-type-mini">
                        <button class="ot-mini ${isLimit?'':'active'}"
                            onclick="setRowOrderType('${m.symbol}','MARKET',this)">MKT</button>
                        <button class="ot-mini ${isLimit?'active':''}"
                            onclick="setRowOrderType('${m.symbol}','LIMIT',this)">LMT</button>
                    </div>
                    <button
                        class="trade-btn buy-btn"
                        onclick="manualBuy('${m.symbol}')"
                    >🟢 Buy</button>
                </div>
                ${isLimit ? `
                <div class="limit-price-row" id="lp-row-${m.symbol}">
                    <span class="lp-label">Limit ₹</span>
                    <input
                        type="number"
                        class="trade-qty-input limit-price-input"
                        id="lp-${m.symbol}"
                        data-symbol="${m.symbol}"
                        value="${savedLP}"
                        step="0.05"
                        placeholder="Price"
                        title="Limit price — leave blank to use top bid"
                        oninput="manualLimitPrice['${m.symbol}']=this.value"
                    />
                    <button class="lp-bid-btn" onclick="setLimitToTopBid('${m.symbol}')" title="Use top bid">= Bid</button>
                    <button class="lp-ask-btn" onclick="setLimitToTopAsk('${m.symbol}')" title="Use top ask">= Ask</button>
                </div>` : ''}
            </div>
        `;
    }
}

// Reset a trade qty input back to its server default
// For exit: restores full held quantity (from input.max)
// For buy: recalculates recommended quantity from stored LTP
function resetTradeQty(symbol, type) {
    const input = document.querySelector(`.${type}-input[data-symbol="${symbol}"]`);
    if (!input) return;

    delete manualTradeQty[`${symbol}_${type}`];

    if (type === 'exit') {
        const fullQty = parseInt(input.max) || 1;
        input.value = fullQty;
    } else {
        const ltp = parseFloat(input.dataset.ltp) || 0;
        const recommended = calculateBuyQuantity(ltp);
        input.value = recommended;
        input.placeholder = recommended;
    }

    // Brief green flash to confirm reset
    input.style.borderColor = 'var(--success-color)';
    input.style.background = 'rgba(16, 185, 129, 0.06)';
    setTimeout(() => {
        input.style.borderColor = '';
        input.style.background = '';
    }, 800);
}

// Flash animation for cells
function flashCell(cell, color) {
    const colors = {
        'green': 'rgba(16, 185, 129, 0.4)',
        'red': 'rgba(239, 68, 68, 0.4)',
        'blue': 'rgba(59, 130, 246, 0.3)'
    };
    
    cell.style.transition = 'none';
    cell.style.backgroundColor = colors[color] || colors.blue;
    setTimeout(() => {
        cell.style.transition = 'background-color 0.5s ease';
        cell.style.backgroundColor = 'transparent';
    }, 100);
}

// Track input changes
document.addEventListener('input', (e) => {
    if (e.target.classList.contains('trade-qty-input')) {
        const symbol = e.target.dataset.symbol;
        const type = e.target.dataset.type;
        manualTradeQty[`${symbol}_${type}`] = e.target.value;
    }
});

// Buy LIQUIDCASE Modal Functions
function setLiquidcaseOrderType(type) {
    document.getElementById('liquidcase-order-type').value = type;
    document.querySelectorAll('#liquidcase-order-type-toggle .ot-btn')
        .forEach(b => b.classList.remove('active'));
    const btn = document.getElementById('liq-ot-' + type.toLowerCase());
    if (btn) btn.classList.add('active');
    // Show/hide limit price row
    const liqLimitRow = document.getElementById('liq-limit-row');
    if (liqLimitRow) {
        liqLimitRow.style.display = type === 'LIMIT' ? 'flex' : 'none';
        if (type === 'LIMIT') {
            // Pre-fill with top bid
            const bidEl = document.getElementById('modal-liquidcase-bid');
            const bidTxt = bidEl ? bidEl.textContent.replace('₹','').trim() : '';
            const inp = document.getElementById('liq-limit-price');
            if (inp && bidTxt && bidTxt !== '—') inp.value = bidTxt;
        }
    }
}

function setLiquidcaseLimitToBid() {
    const bidEl = document.getElementById('modal-liquidcase-bid');
    const bidTxt = bidEl ? bidEl.textContent.replace('₹','').trim() : '';
    const inp = document.getElementById('liq-limit-price');
    if (inp && bidTxt && bidTxt !== '—') inp.value = bidTxt;
}

function setLiquidcaseLimitToAsk() {
    const askEl = document.getElementById('modal-liquidcase-ask');
    const askTxt = askEl ? askEl.textContent.replace('₹','').trim() : '';
    const inp = document.getElementById('liq-limit-price');
    if (inp && askTxt && askTxt !== '—') inp.value = askTxt;
}

function showBuyLiquidcaseModal() {
    const modal = document.getElementById('buy-liquidcase-modal');
    modal.style.display = 'flex';

    // Always reset to LIMIT on open
    setLiquidcaseOrderType('LIMIT');
    const lpInpReset = document.getElementById('liq-limit-price');
    if (lpInpReset) lpInpReset.value = '';

    // Reset bid/ask to loading state
    const bidEl = document.getElementById('modal-liquidcase-bid');
    const askEl = document.getElementById('modal-liquidcase-ask');
    if (bidEl) bidEl.textContent = '⏳';
    if (askEl) askEl.textContent = '⏳';

    // Fetch cash balance — retry once if result is 0 (Ulaa/browser cache issue)
    const cashEl = document.getElementById('modal-cash-balance');
    // Prefill from inline card if already showing a real value
    const inlineVal = document.getElementById('liquidcase-avail-cash')?.textContent;
    if (cashEl && inlineVal && inlineVal !== '₹—' && inlineVal !== '₹0.00') {
        cashEl.textContent = inlineVal;
    } else if (cashEl) {
        cashEl.textContent = 'Loading…';
    }
    _lastCashFetch = 0; // force fresh fetch for the modal
    const fetchCash = (attempt = 1) => fetch('/api/cash-balance', { cache: 'no-store' })
        .then(r => r.json())
        .then(data => {
            const cashBalance = data.available_cash ?? data.live_balance ?? 0;
            if (data.error && !cashBalance) {
                // Hard failure with no cached value — show "Unavailable"
                if (cashEl) cashEl.textContent = 'Unavailable';
                return;
            }
            if (cashBalance === 0 && attempt < 2 && !data.stale) {
                setTimeout(() => fetchCash(2), 1500);
                return;
            }
            const formatted = `₹${cashBalance.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
            // Append stale indicator so user knows it may not be live
            const display = data.stale ? `${formatted} ⚠` : formatted;
            if (cashEl) {
                cashEl.textContent = display;
                cashEl.title = data.stale ? 'Cached value — session reconnecting' : 'Live balance';
            }
            // Keep inline card in sync
            const inlineEl = document.getElementById('liquidcase-avail-cash');
            if (inlineEl) { inlineEl.textContent = formatted; _lastCashFetch = Date.now(); }
        })
        .catch(() => { if (cashEl) cashEl.textContent = 'Error loading'; });
    fetchCash();

    // LTP from portfolio cache
    const ltp = portfolioData?.liquidcase?.price || 0;
    document.getElementById('modal-liquidcase-price').textContent =
        ltp > 0 ? `₹${ltp.toFixed(2)}` : '₹0.00';

    // Fetch live market depth for LIQUIDCASE
    fetch('/api/depth/LIQUIDCASE')
        .then(r => r.json())
        .then(d => {
            if (bidEl) bidEl.textContent = d.top_bid ? `₹${Number(d.top_bid).toFixed(2)}` : '—';
            if (askEl) askEl.textContent = d.top_ask ? `₹${Number(d.top_ask).toFixed(2)}` : '—';

            // Also update LTP if REST gave us a better price
            if (d.ltp && d.ltp > 0) {
                document.getElementById('modal-liquidcase-price').textContent = `₹${Number(d.ltp).toFixed(2)}`;
            }

            // Pre-fill limit price input if LIMIT mode is selected
            if (document.getElementById('liquidcase-order-type')?.value === 'LIMIT' && d.top_bid) {
                const lpInp = document.getElementById('liq-limit-price');
                if (lpInp && !lpInp.value) lpInp.value = Number(d.top_bid).toFixed(2);
            }
        })
        .catch(() => {
            if (bidEl) bidEl.textContent = '—';
            if (askEl) askEl.textContent = '—';
        });
}

function closeBuyLiquidcaseModal() {
    const modal = document.getElementById('buy-liquidcase-modal');
    modal.style.display = 'none';
    document.getElementById('custom-liquidcase-amount').value = '';
}

function _getLiquidcaseOrderType() {
    return document.getElementById('liquidcase-order-type')?.value || 'MARKET';
}

function _getLiquidcaseLimitPrice() {
    // Prefer the editable limit price input
    const inp = document.getElementById('liq-limit-price');
    if (inp && inp.value) {
        const v = parseFloat(inp.value);
        if (!isNaN(v) && v > 0) return v;
    }
    // Fallback: top bid display
    const bidEl = document.getElementById('modal-liquidcase-bid');
    if (!bidEl) return null;
    const txt = bidEl.textContent.replace('₹','').trim();
    const v = parseFloat(txt);
    return isNaN(v) ? null : v;
}

function buyLiquidcaseAll() {
    if (!confirm('Buy LIQUIDCASE with ALL available cash?')) {
        return;
    }
    
    showToast('Placing order for ALL cash...', 'info');
    
    const liqOT1    = _getLiquidcaseOrderType();
    const liqLmt1   = liqOT1 === 'LIMIT' ? _getLiquidcaseLimitPrice() : null;
    fetch('/api/buy-liquidcase', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ use_all_cash: true, order_type: liqOT1, limit_price: liqLmt1 })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showToast(data.message, 'success');
                closeBuyLiquidcaseModal();
                setTimeout(() => {
                    loadPortfolio();
                    loadMarketData();
                }, 1000);
            } else {
                showToast((data.zerodha_error ? '🔴 Zerodha: ' : '❌ ') + (data.error || 'Failed to buy LIQUIDCASE'), 'error');
            }
        })
        .catch(error => {
            console.error('Error buying LIQUIDCASE:', error);
            showToast('Failed to buy LIQUIDCASE', 'error');
        });
}

function buyLiquidcaseCustom() {
    const amount = parseFloat(document.getElementById('custom-liquidcase-amount').value);
    
    if (!amount || amount <= 0) {
        showToast('Please enter a valid amount', 'error');
        return;
    }
    
    if (!confirm(`Buy LIQUIDCASE worth ₹${amount.toLocaleString('en-IN')}?`)) {
        return;
    }
    
    showToast(`Placing order for ₹${amount.toLocaleString('en-IN')}...`, 'info');
    
    const liqOT2    = _getLiquidcaseOrderType();
    const liqLmt2   = liqOT2 === 'LIMIT' ? _getLiquidcaseLimitPrice() : null;
    fetch('/api/buy-liquidcase', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ amount, order_type: liqOT2, limit_price: liqLmt2 })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showToast(data.message, 'success');
                closeBuyLiquidcaseModal();
                setTimeout(() => {
                    loadPortfolio();
                    loadMarketData();
                }, 1000);
            } else {
                showToast((data.zerodha_error ? '🔴 Zerodha: ' : '❌ ') + (data.error || 'Failed to buy LIQUIDCASE'), 'error');
            }
        })
        .catch(error => {
            console.error('Error buying LIQUIDCASE:', error);
            showToast('Failed to buy LIQUIDCASE', 'error');
        });
}

// ── Sell LIQUIDCASE ─────────────────────────────────────────
function showSellLiquidcaseModal() {
    const modal = document.getElementById('sell-liquidcase-modal');
    modal.style.display = 'flex';

    // Always reset to LIMIT on open
    setSellLiquidcaseOrderType('LIMIT');
    const lpInpReset = document.getElementById('sell-liq-limit-price');
    if (lpInpReset) lpInpReset.value = '';

    const bidEl = document.getElementById('sell-modal-liquidcase-bid');
    const askEl = document.getElementById('sell-modal-liquidcase-ask');
    if (bidEl) bidEl.textContent = '⏳';
    if (askEl) askEl.textContent = '⏳';

    // Units held from portfolio cache
    const qty = portfolioData?.liquidcase?.quantity || 0;
    const ltp = portfolioData?.liquidcase?.price || 0;
    const marketValue = qty * ltp;
    document.getElementById('sell-modal-liquidcase-qty').textContent =
        marketValue > 0
            ? `₹${marketValue.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})} (${qty} units)`
            : '₹0.00';

    // LTP from portfolio cache
    document.getElementById('sell-modal-liquidcase-price').textContent =
        ltp > 0 ? `₹${ltp.toFixed(2)}` : '₹0.00';

    // Live market depth
    fetch('/api/depth/LIQUIDCASE')
        .then(r => r.json())
        .then(d => {
            if (bidEl) bidEl.textContent = d.top_bid ? `₹${Number(d.top_bid).toFixed(2)}` : '—';
            if (askEl) askEl.textContent = d.top_ask ? `₹${Number(d.top_ask).toFixed(2)}` : '—';
            if (d.ltp && d.ltp > 0) {
                document.getElementById('sell-modal-liquidcase-price').textContent = `₹${Number(d.ltp).toFixed(2)}`;
                // Refresh market value with live LTP
                const liveQty = portfolioData?.liquidcase?.quantity || 0;
                const liveMV  = liveQty * Number(d.ltp);
                document.getElementById('sell-modal-liquidcase-qty').textContent =
                    liveMV > 0
                        ? `₹${liveMV.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})} (${liveQty} units)`
                        : '₹0.00';
            }
            if (document.getElementById('sell-liquidcase-order-type')?.value === 'LIMIT' && d.top_bid) {
                const lpInp = document.getElementById('sell-liq-limit-price');
                if (lpInp && !lpInp.value) lpInp.value = Number(d.top_bid).toFixed(2);
            }
        })
        .catch(() => {
            if (bidEl) bidEl.textContent = '—';
            if (askEl) askEl.textContent = '—';
        });
}

function closeSellLiquidcaseModal() {
    const modal = document.getElementById('sell-liquidcase-modal');
    modal.style.display = 'none';
    document.getElementById('custom-sell-liquidcase-amount').value = '';
}

function setSellLiquidcaseOrderType(type) {
    document.getElementById('sell-liquidcase-order-type').value = type;
    document.getElementById('sell-liq-ot-market').classList.toggle('active', type === 'MARKET');
    document.getElementById('sell-liq-ot-limit').classList.toggle('active', type === 'LIMIT');
    document.getElementById('sell-liq-limit-row').style.display = type === 'LIMIT' ? 'flex' : 'none';
}

function setSellLiquidcaseLimitToBid() {
    const bidEl = document.getElementById('sell-modal-liquidcase-bid');
    if (!bidEl) return;
    const v = parseFloat(bidEl.textContent.replace('₹', '').trim());
    if (!isNaN(v)) document.getElementById('sell-liq-limit-price').value = v.toFixed(2);
}

function setSellLiquidcaseLimitToAsk() {
    const askEl = document.getElementById('sell-modal-liquidcase-ask');
    if (!askEl) return;
    const v = parseFloat(askEl.textContent.replace('₹', '').trim());
    if (!isNaN(v)) document.getElementById('sell-liq-limit-price').value = v.toFixed(2);
}

function _getSellLiquidcaseOrderType() {
    return document.getElementById('sell-liquidcase-order-type')?.value || 'MARKET';
}

function _getSellLiquidcaseLimitPrice() {
    const inp = document.getElementById('sell-liq-limit-price');
    if (inp && inp.value) {
        const v = parseFloat(inp.value);
        if (!isNaN(v) && v > 0) return v;
    }
    return null;
}

function sellLiquidcaseAll() {
    const qty = portfolioData?.liquidcase?.quantity || 0;
    if (qty <= 0) {
        showToast('No LIQUIDCASE units to sell', 'error');
        return;
    }
    if (!confirm(`Sell ALL ${qty} LIQUIDCASE units?`)) return;

    showToast(`Placing sell order for all ${qty} units...`, 'info');
    const ot = _getSellLiquidcaseOrderType();
    const lmt = ot === 'LIMIT' ? _getSellLiquidcaseLimitPrice() : null;
    fetch('/api/sell-liquidcase', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sell_all: true, order_type: ot, limit_price: lmt })
    })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                showToast(data.message, 'success');
                closeSellLiquidcaseModal();
                setTimeout(() => { loadPortfolio(); loadMarketData(); }, 1000);
            } else {
                showToast((data.zerodha_error ? '🔴 Zerodha: ' : '❌ ') + (data.error || 'Failed to sell LIQUIDCASE'), 'error');
            }
        })
        .catch(() => showToast('Failed to sell LIQUIDCASE', 'error'));
}

function sellLiquidcaseCustom() {
    const amount = parseFloat(document.getElementById('custom-sell-liquidcase-amount').value);
    if (!amount || amount <= 0) {
        showToast('Please enter a valid amount (₹)', 'error');
        return;
    }

    const ot = _getSellLiquidcaseOrderType();
    const lmt = ot === 'LIMIT' ? _getSellLiquidcaseLimitPrice() : null;

    // Derive unit price: use limit price if set, else LTP from modal
    const ltpText = document.getElementById('sell-modal-liquidcase-price')?.textContent || '';
    const ltp = parseFloat(ltpText.replace('₹', '').trim()) || 0;
    const unitPrice = (ot === 'LIMIT' && lmt) ? lmt : ltp;

    if (!unitPrice || unitPrice <= 0) {
        showToast('Unable to determine unit price — please enter a limit price', 'error');
        return;
    }

    const qty = Math.floor(amount / unitPrice);
    if (qty <= 0) {
        showToast(`Amount ₹${amount} is less than 1 unit price (₹${unitPrice.toFixed(2)})`, 'error');
        return;
    }

    if (!confirm(`Sell ${qty} LIQUIDCASE unit(s) for ~₹${(qty * unitPrice).toFixed(2)}?`)) return;

    showToast(`Placing sell order for ${qty} unit(s)...`, 'info');
    fetch('/api/sell-liquidcase', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ quantity: qty, order_type: ot, limit_price: lmt })
    })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                showToast(data.message, 'success');
                closeSellLiquidcaseModal();
                setTimeout(() => { loadPortfolio(); loadMarketData(); }, 1000);
            } else {
                showToast((data.zerodha_error ? '🔴 Zerodha: ' : '❌ ') + (data.error || 'Failed to sell LIQUIDCASE'), 'error');
            }
        })
        .catch(() => showToast('Failed to sell LIQUIDCASE', 'error'));
}

// Close modal when clicking outside
window.onclick = function(event) {
    const liquidcaseModal = document.getElementById('buy-liquidcase-modal');
    const sellLiquidcaseModal = document.getElementById('sell-liquidcase-modal');
    const holdingsModal = document.getElementById('holdings-modal');
    
    if (event.target === liquidcaseModal) {
        closeBuyLiquidcaseModal();
    }
    if (event.target === sellLiquidcaseModal) {
        closeSellLiquidcaseModal();
    }
    if (event.target === holdingsModal) {
        closeHoldingsModal();
    }
};

// Auto-Buy LIQUIDCASE Functions
function startAutoBuy() {
    // Stop existing interval if any
    stopAutoBuy();
    
    // Check for pending purchases from previous sessions
    const recentPurchases = getRecentPurchases();
    if (recentPurchases.length > 0) {
        const totalPending = getTotalRecentPurchases();
        console.log(`🤖 Auto-Buy LIQUIDCASE started (${recentPurchases.length} pending purchase(s): ₹${totalPending.toFixed(2)})`);
    } else {
        console.log('🤖 Auto-Buy LIQUIDCASE started');
    }
    
    // Delay first check by 10 seconds to allow dashboard to load fresh data
    // (prevents buying with stale/cached price data on page load)
    setTimeout(() => {
        checkAndBuyLiquidcase();
    }, 10000);
    
    // Then check every 60 seconds after that
    autoBuyInterval = setInterval(() => {
        checkAndBuyLiquidcase();
    }, 60000);
}

function stopAutoBuy() {
    if (autoBuyInterval) {
        clearInterval(autoBuyInterval);
        autoBuyInterval = null;
        console.log('🤖 Auto-Buy LIQUIDCASE stopped');
    }
}

function checkAndBuyLiquidcase() {
    if (!autoBuyEnabled) return;
    
    console.log('🤖 Checking cash balance for auto-buy...');
    
    // Fetch both cash balance and LIQUIDCASE price
    Promise.all([
        fetch('/api/cash-balance').then(r => r.json()),
        fetch('/api/market').then(r => r.json())
    ])
        .then(([cashData, marketData]) => {
            const apiCashBalance = cashData.available_cash || 0;
            
            // Get recent purchases not yet reflected in Zerodha API
            const recentPurchases = getRecentPurchases();
            const recentPurchasesTotal = getTotalRecentPurchases();
            
            // Calculate TRUE available cash (accounting for recent purchases)
            const trueCashBalance = apiCashBalance - recentPurchasesTotal;
            
            // Get LIQUIDCASE LTP from portfolioData (WebSocket live price or holdings fallback)
            let liquidcaseLtp = portfolioData?.liquidcase?.price || 0;
            if (liquidcaseLtp <= 0) {
                console.warn('⚠️ LIQUIDCASE price unavailable — skipping auto-buy to avoid miscalculation');
                return;
            }
            
            // Dynamic threshold with buffer: need enough for at least 1 unit + 5% buffer
            const minThreshold = liquidcaseLtp * 1.05;
            
            console.log(`💰 Zerodha API cash: ₹${apiCashBalance.toFixed(2)}`);
            if (recentPurchasesTotal > 0) {
                console.log(`⏳ Recent purchases (pending sync): ₹${recentPurchasesTotal.toFixed(2)} (${recentPurchases.length} purchase(s))`);
                console.log(`💵 TRUE available cash: ₹${trueCashBalance.toFixed(2)}`);
            }
            console.log(`📊 LIQUIDCASE LTP: ₹${liquidcaseLtp.toFixed(2)}`);
            console.log(`🎯 Min needed: ₹${minThreshold.toFixed(2)} (1 unit + 5% buffer)`);
            
            // Check TRUE balance (not stale API balance)
            if (trueCashBalance < minThreshold) {
                if (recentPurchasesTotal > 0) {
                    console.log(`⏸ TRUE cash (₹${trueCashBalance.toFixed(2)}) below threshold, skipping (accounting for recent purchases)`);
                } else {
                    console.log(`⏸ Cash (₹${trueCashBalance.toFixed(2)}) below threshold, skipping auto-buy`);
                }
                return; // Exit early, don't try to buy
            }
            
            // Calculate how many units we can buy with TRUE balance
            const affordableQty = Math.floor(trueCashBalance / liquidcaseLtp);
            
            if (affordableQty <= 0) {
                console.log(`⏸ Cannot afford even 1 unit (₹${liquidcaseLtp.toFixed(2)}), skipping`);
                return; // Exit early
            }
            
            const purchaseAmount = affordableQty * liquidcaseLtp;
            console.log(`🤖 Auto-buying ${affordableQty} LIQUIDCASE units (₹${purchaseAmount.toFixed(2)})`);
            
            // Place auto-buy order
            fetch('/api/buy-liquidcase', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ use_all_cash: true })
            })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        // Track this purchase (prevents buying again before Zerodha syncs)
                        addRecentPurchase(purchaseAmount, data.quantity);
                        
                        console.log(`✅ Auto-buy successful: ${data.message}`);
                        console.log(`📝 Tracked purchase: ₹${purchaseAmount.toFixed(2)} (will sync with Zerodha in ~5 min)`);
                        showToast(`🤖 Auto-bought ${data.quantity} LIQUIDCASE units`, 'success');
                        
                        // Refresh portfolio after 2 seconds
                        setTimeout(() => {
                            loadPortfolio();
                            loadMarketData();
                        }, 2000);
                    } else {
                        console.error(`❌ Auto-buy failed: ${data.error}`);
                    }
                })
                .catch(error => {
                    console.error('Auto-buy API error:', error);
                });
        })
        .catch(error => {
            console.error('Error fetching data for auto-buy:', error);
        });
}

function updateAutoBuyIndicator() {
    const indicator = document.getElementById('auto-buy-indicator');
    if (indicator) {
        indicator.style.display = autoBuyEnabled ? 'inline-block' : 'none';
    }
}

// Load signals
function loadSignals() {
    // Signals are now integrated into the Market Monitor table's Action column
    // No need for separate sidebar panel
}

// Load market indices
function loadIndices() {
    fetch('/api/indices')
        .then(response => response.json())
        .then(data => {
            updateIndicesTicker(data);
        })
        .catch(error => console.error('Error loading indices:', error));
}

// Store previous values for change detection
const previousValues = {
    indices: {}
};

// Cache for index elements
const indexCache = new Map();

// Update indices ticker with INCREMENTAL updates
function updateIndicesTicker(indices) {
    const indexMap = {
        'NIFTY 50':         'index-nifty50',
        'NIFTY MIDCAP 150': 'index-midcap150',
        'INDIA VIX':        'index-indiavix'
    };
    
    indices.forEach(index => {
        const elementId = indexMap[index.name];
        if (!elementId) return;
        
        // Get or cache elements
        let cached = indexCache.get(index.name);
        if (!cached) {
            const element = document.getElementById(elementId);
            if (!element) return;
            
            cached = {
                element: element,
                valueElement: element.querySelector('.index-value'),
                changeElement: element.querySelector('.index-change'),
                lastValue: null,
                lastChange: null
            };
            indexCache.set(index.name, cached);
        }
        
        const { valueElement, changeElement } = cached;
        
        // Update value only if changed (show prev_close if market closed)
        const displayLtp = index.ltp > 0 ? index.ltp : (index.prev_close || 0);
        if (valueElement && displayLtp > 0) {
            const newValue = displayLtp.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2});
            
            if (cached.lastValue !== newValue) {
                // Flash effect on change
                if (cached.lastValue !== null) {
                    const flashColor = index.change >= 0 ? 'rgba(16, 185, 129, 0.3)' : 'rgba(239, 68, 68, 0.3)';
                    valueElement.style.transition = 'none';
                    valueElement.style.backgroundColor = flashColor;
                    setTimeout(() => {
                        valueElement.style.transition = 'background-color 0.5s ease';
                        valueElement.style.backgroundColor = 'transparent';
                    }, 100);
                }
                
                valueElement.textContent = newValue;
                cached.lastValue = newValue;
            }
        }

        // Update change only if changed
        if (changeElement && displayLtp > 0) {
            const changeSign = index.change >= 0 ? '+' : '';
            const pctSign = index.change_pct >= 0 ? '+' : '';
            const newChange = `${changeSign}${index.change.toFixed(2)} (${pctSign}${index.change_pct.toFixed(2)}%)`;
            
            // Suffix when market is closed
            const closedSuffix = index.market_closed ? ' ◦' : '';
            const displayChange = newChange + closedSuffix;

            if (cached.lastChange !== displayChange) {
                changeElement.textContent = displayChange;
                
                // Update class only if needed
                const newClass = index.change > 0 ? 'positive' : (index.change < 0 ? 'negative' : 'neutral');
                const currentClass = changeElement.classList.contains('positive') ? 'positive' : 
                                   (changeElement.classList.contains('negative') ? 'negative' : 'neutral');
                
                if (currentClass !== newClass) {
                    changeElement.classList.remove('positive', 'negative', 'neutral');
                    changeElement.classList.add(newClass);
                }
                
                cached.lastChange = displayChange;
            }
        }
    });
}

// Load logs
function loadLogs() {
    fetch('/api/logs')
        .then(response => response.json())
        .then(data => {
            updateLogsPanel(data);
        })
        .catch(error => console.error('Error loading logs:', error));
}

// Update logs panel
function updateLogsPanel(logs) {
    const container = document.getElementById('logs-container');
    
    if (!logs || logs.length === 0) {
        container.innerHTML = '<p class="empty-state">No logs available</p>';
        return;
    }
    
    container.innerHTML = logs.map(log => 
        `<div class="log-entry">${escapeHtml(log.text)}</div>`
    ).join('');
    
    // Scroll to bottom
    container.scrollTop = container.scrollHeight;
}

// ── Strategy Signal Summary (Activity tab) ────────────────────────────────────
// Called whenever market data or BnH status is refreshed.
// Reads marketDataCache + bnhLatestCache and renders a concise status card.
function updateSignalSummary() {
    const body = document.getElementById('signal-summary-body');
    const timeEl = document.getElementById('signal-summary-time');
    if (!body) return;

    const now = new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    if (timeEl) timeEl.textContent = `as of ${now}`;

    const rows = [];

    // ── Active Strategy ───────────────────────────────────────────────────────
    const mkt = marketDataCache || [];
    if (mkt.length === 0) {
        rows.push(_sigRow('none', 'Active Strategy', '⏳', 'Waiting for market data…', []));
    } else {
        const profitTarget  = currentProfitTarget || 5;
        const minPriceDrop  = parseFloat(document.getElementById('bot-min-price-drop')?.value) || 1.0;
        const nearSellPct   = 1.0;   // show if within this many % points of target
        const nearWrPoints  = 20;    // show if W%R within this many points of threshold

        // ── Definite signals (action already set by backend) ──────────────────
        const buys  = mkt.filter(m => m.action === 'BUY');
        const sells = mkt.filter(m => m.action === 'SELL');
        const waits = mkt.filter(m => m.action === 'WAIT');

        // ── Near-sell: held, not yet at target but within nearSellPct ─────────
        const nearSell = mkt.filter(m =>
            m.is_held &&
            m.profit_pct != null &&
            m.profit_pct >= (profitTarget - nearSellPct) &&
            m.profit_pct < profitTarget
        );

        // ── Near next buy (first slot): not held, W%R within nearWrPoints of threshold ──
        const nearBuyFirst = mkt.filter(m =>
            !m.is_held &&
            m.williams_r != null &&
            m.williams_r > activeWrThreshold &&
            m.williams_r <= (activeWrThreshold + nearWrPoints)
        );

        // ── Near next buy (additional slot): held, ltp within minPriceDrop of avg ──
        const nearBuyAdditional = mkt.filter(m =>
            m.is_held &&
            m.action === 'HOLD' &&
            m.avg_price != null &&
            m.ltp != null &&
            m.profit_pct != null &&
            m.profit_pct < 0 &&   // only show if below avg price
            Math.abs(m.profit_pct) >= (minPriceDrop - 1.0) &&
            Math.abs(m.profit_pct) < minPriceDrop
        );

        const chips = [
            ...buys.map(m => `<span class="sig-chip chip-buy">BUY ${m.symbol}</span>`),
            ...sells.map(m => `<span class="sig-chip chip-sell">SELL ${m.symbol}</span>`),
            ...waits.map(m => `<span class="sig-chip chip-wait">WAIT ${m.symbol} (no slot)</span>`),
        ];

        const parts = [];
        if (buys.length)  parts.push(`${buys.length} buy signal${buys.length > 1 ? 's' : ''}`);
        if (sells.length) parts.push(`${sells.length} sell signal${sells.length > 1 ? 's' : ''}`);
        if (waits.length) parts.push(`${waits.length} waiting for slot`);

        if (chips.length > 0) {
            rows.push(_sigRow('ok', 'Active Strategy', '✅', parts.join(' · '), chips));
        } else {
            rows.push(_sigRow('none', 'Active Strategy', '😐', 'No buy or sell signals right now.', []));
        }

        // ── Near Sell Target ─────────────────────────────────────────────────
        if (nearSell.length > 0) {
            const nearSellChips = nearSell.map(m => {
                const gap = (profitTarget - m.profit_pct).toFixed(2);
                return `<span class="sig-chip chip-sell">📈 ${m.symbol} P/L ${m.profit_pct >= 0 ? '+' : ''}${m.profit_pct.toFixed(2)}% → target ${profitTarget}% (${gap}% away)</span>`;
            });
            rows.push(_sigRow('warn', 'Near Sell Target', '🎯',
                `${nearSell.length} holding${nearSell.length > 1 ? 's' : ''} within ${nearSellPct}% of sell target (${profitTarget}%)`,
                nearSellChips));
        }

        // ── Near Buy Slot (first entry) ──────────────────────────────────────
        if (nearBuyFirst.length > 0) {
            const nbChips = nearBuyFirst.map(m => {
                const gap = (m.williams_r - activeWrThreshold).toFixed(1);
                return `<span class="sig-chip chip-wait">📉 ${m.symbol} W%R ${m.williams_r.toFixed(1)} (${gap} from ${activeWrThreshold})</span>`;
            });
            rows.push(_sigRow('warn', 'Near Buy Signal', '📊',
                `${nearBuyFirst.length} symbol${nearBuyFirst.length > 1 ? 's' : ''} approaching oversold threshold (W%R within ${nearWrPoints} pts of ${activeWrThreshold})`,
                nbChips));
        }

        // ── Near Additional Slot (re-buy) ─────────────────────────────────────
        if (nearBuyAdditional.length > 0) {
            const naChips = nearBuyAdditional.map(m => {
                const dropPct = Math.abs(m.profit_pct).toFixed(2);
                return `<span class="sig-chip chip-wait">🔄 ${m.symbol} −${dropPct}% below avg (slot trigger at −${minPriceDrop}%)</span>`;
            });
            rows.push(_sigRow('warn', 'Near Slot Re-buy', '🔄',
                `${nearBuyAdditional.length} holding${nearBuyAdditional.length > 1 ? 's' : ''} approaching next slot buy level (min drop ${minPriceDrop}%)`,
                naChips));
        }
    }

    // ── Dip Accumulator & Harvester (multi-symbol) ───────────────────────────────
    const bnhD = bnhLatestCache;   // full status object from loadBnHStatus
    if (!bnhD) {
        rows.push(_sigRow('none', 'Dip Accumulator & Harvester', '⏳', 'Waiting for engine data…', []));
    } else {
        const symStatus  = bnhD.symbols_status || {};
        const syms       = bnhD.symbols || (bnhD.symbol ? [bnhD.symbol] : []);
        const partialPct = bnhHarvestPct;

        if (!syms.length) {
            rows.push(_sigRow('none', 'Dip Accumulator & Harvester', '😐', 'No symbols configured in BnH universe.', []));
        } else {
            const chips     = [];
            let   hasSignal = false;

            syms.forEach(sym => {
                const s      = symStatus[sym] || bnhD.latest || {};
                const wr     = s.wr != null ? Number(s.wr).toFixed(1) : null;
                const state  = s.wr_state || '';
                const bought = s.bought_today === true;
                const h      = s.holdings || {};
                const pnlPct = h.pnl_pct != null ? Number(h.pnl_pct) : null;

                if (state === 'OVERSOLD') {
                    hasSignal = true;
                    chips.push(bought
                        ? `<span class="sig-chip chip-buy">✅ ${sym} accumulated today (W%R ${wr})</span>`
                        : `<span class="sig-chip chip-buy">BUY ${sym} @ 3:15 PM · W%R ${wr}</span>`);
                } else if (pnlPct != null && pnlPct >= partialPct && (h.qty || 0) > 0) {
                    hasSignal = true;
                    chips.push(`<span class="sig-chip chip-sell">💰 HARVEST ${sym} — P&L +${pnlPct.toFixed(2)}%</span>`);
                } else if (pnlPct != null && pnlPct > 0 && (h.qty || 0) > 0) {
                    const gap = (partialPct - pnlPct).toFixed(2);
                    chips.push(`<span class="sig-chip chip-wait">📈 ${sym} +${pnlPct.toFixed(2)}% · harvest at ${partialPct}% (${gap}% away)</span>`);
                } else {
                    chips.push(`<span class="sig-chip chip-wait">😐 ${sym} W%R ${wr ?? '—'}</span>`);
                }
            });

            const oversoldList = syms.filter(s => (symStatus[s] || bnhD.latest || {}).wr_state === 'OVERSOLD');
            const harvestList  = syms.filter(s => {
                const h = ((symStatus[s] || bnhD.latest || {}).holdings || {});
                return h.pnl_pct != null && h.pnl_pct >= partialPct && (h.qty || 0) > 0;
            });

            if (hasSignal) {
                const summary = [];
                if (oversoldList.length) summary.push(`${oversoldList.length} buy signal${oversoldList.length > 1 ? 's' : ''}`);
                if (harvestList.length)  summary.push(`${harvestList.length} harvest trigger${harvestList.length > 1 ? 's' : ''}`);
                rows.push(_sigRow('ok', 'Dip Accumulator & Harvester', '✅', summary.join(' · '), chips));
            } else {
                rows.push(_sigRow('none', 'Dip Accumulator & Harvester', '😐',
                    `No accumulation or harvest signals across ${syms.length} symbol${syms.length > 1 ? 's' : ''}.`, chips));
            }
        }
    }

    body.innerHTML = rows.join('');
}

function _sigRow(type, strategy, icon, text, chips) {
    const chipsHtml = chips.length
        ? `<div class="sig-chips">${chips.join('')}</div>`
        : '';
    return `
        <div class="sig-row sig-${type}">
            <span class="sig-icon">${icon}</span>
            <span class="sig-strategy">${strategy}</span>
            <span class="sig-text">${escapeHtml(text)}${chipsHtml}</span>
        </div>`;
}
// ─────────────────────────────────────────────────────────────────────────────

// Start bot
function startBot() {
    const startBtn = document.getElementById('btn-start');
    if (startBtn) { startBtn.disabled = true; startBtn.textContent = '⏳ Starting…'; }

    fetch('/api/bot/start', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.error) {
                showToast(`Failed to start bot: ${data.error}`, 'error');
                if (startBtn) { startBtn.disabled = false; startBtn.textContent = '▶ START BOT'; }
                return;
            }

            botRunning = true;
            updateBotButtons();

            const botStatusBadge = document.getElementById('bot-status-badge');
            if (botStatusBadge) {
                botStatusBadge.textContent = '▶ RUNNING';
                botStatusBadge.style.backgroundColor = '#10b981';
            }

            const profitSelect = document.getElementById('profit-select');
            const quantityInput = document.getElementById('test-quantity');
            const profit = profitSelect ? profitSelect.value + '%' : 'default';
            const qty = quantityInput ? (parseInt(quantityInput.value) || 0) : 0;
            const qtyText = qty === 0 ? 'No Limit' : qty + ' units';
            const isDry = document.getElementById('mode-badge')?.textContent?.includes('DRY');
            const modeLabel = isDry ? ' [DRY RUN — no real orders]' : ' [LIVE]';

            showToast(`Bot Started${modeLabel} | Profit: ${profit} | Qty: ${qtyText}`, 'success');
        })
        .catch(error => {
            console.error('Error starting bot:', error);
            showToast('Failed to start bot', 'error');
            if (startBtn) { startBtn.disabled = false; startBtn.textContent = '▶ START BOT'; }
        });
}

// Stop bot
function stopBot() {
    const pauseBtn = document.getElementById('btn-pause');
    if (pauseBtn) { pauseBtn.disabled = true; pauseBtn.textContent = '⏳ Stopping…'; }

    fetch('/api/bot/stop', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.error) {
                showToast(`Failed to stop bot: ${data.error}`, 'error');
                if (pauseBtn) { pauseBtn.disabled = false; pauseBtn.textContent = '⏸ PAUSE'; }
                return;
            }

            botRunning = false;
            updateBotButtons();

            const botStatusBadge = document.getElementById('bot-status-badge');
            if (botStatusBadge) {
                botStatusBadge.textContent = '⏸ PAUSED';
                botStatusBadge.style.backgroundColor = '#6b7280';
            }

            showToast('Trading bot paused', 'warning');
        })
        .catch(error => {
            console.error('Error stopping bot:', error);
            showToast('Failed to stop bot', 'error');
            if (pauseBtn) { pauseBtn.disabled = false; pauseBtn.textContent = '⏸ PAUSE'; }
        });
}

// Sync portfolio
// Per-row order type overrides: {symbol: 'MARKET'|'LIMIT'}
const manualOrderType  = {};
// Per-row limit prices: {symbol: '123.45'}
const manualLimitPrice = {};
// Throttle tracker for auto limit-price refresh: {symbol: lastRefreshTimestamp}
const _limitRefreshTs = {};
const LIMIT_REFRESH_INTERVAL_MS = 3000;  // refresh top-bid every 3 s per symbol

function setLimitToTopBid(symbol) {
    const row = marketDataCache?.find(r => r.symbol === symbol);
    if (row?.top_bid) {
        manualLimitPrice[symbol] = row.top_bid.toFixed(2);
        const inp = document.getElementById(`lp-${symbol}`);
        if (inp) {
            inp.value = manualLimitPrice[symbol];
            delete inp.dataset.userEdited;   // re-enable auto-refresh
            delete _limitRefreshTs[symbol];  // fire immediately on next cycle
        }
    }
}

function setLimitToTopAsk(symbol) {
    const row = marketDataCache?.find(r => r.symbol === symbol);
    if (row?.top_ask) {
        manualLimitPrice[symbol] = row.top_ask.toFixed(2);
        const inp = document.getElementById(`lp-${symbol}`);
        if (inp) inp.value = manualLimitPrice[symbol];
    }
}

function refreshDepth(symbol) {
    fetch(`/api/depth/${symbol}`)
        .then(r => r.json())
        .then(d => {
            // Update cache
            const row = marketDataCache?.find(r => r.symbol === symbol);
            if (row) {
                row.top_bid = d.top_bid;
                row.top_ask = d.top_ask;
            }
            // Update depth strip inline without full table rebuild
            const ctrl = document.getElementById(`buy-ctrl-${symbol}`);
            if (ctrl) {
                const bidEl = ctrl.querySelector('.depth-bid');
                const askEl = ctrl.querySelector('.depth-ask');
                if (bidEl) bidEl.textContent = `B:${d.top_bid ? '₹'+d.top_bid.toFixed(2) : '—'}`;
                if (askEl) askEl.textContent = `A:${d.top_ask ? '₹'+d.top_ask.toFixed(2) : '—'}`;
            }
            // Auto-populate limit price input if LIMIT mode
            if (manualOrderType[symbol] === 'LIMIT' && d.top_bid) {
                manualLimitPrice[symbol] = d.top_bid.toFixed(2);
                const inp = document.getElementById(`lp-${symbol}`);
                if (inp && !inp.dataset.userEdited) inp.value = manualLimitPrice[symbol];
            }
        })
        .catch(() => {});
}

function setRowOrderType(symbol, type, btn) {
    manualOrderType[symbol] = type;
    // Update sibling buttons
    const group = btn.closest('.order-type-mini');
    if (group) group.querySelectorAll('.ot-mini').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');

    // Show/hide limit price row
    const ctrl = document.getElementById(`buy-ctrl-${symbol}`);
    if (!ctrl) return;
    let lpRow = document.getElementById(`lp-row-${symbol}`);

    if (type === 'LIMIT') {
        if (!lpRow) {
            // Create limit price row dynamically
            lpRow = document.createElement('div');
            lpRow.className = 'limit-price-row';
            lpRow.id = `lp-row-${symbol}`;
            const row = marketDataCache?.find(r => r.symbol === symbol);
            const topBid = row?.top_bid ? row.top_bid.toFixed(2) : (row?.ltp ? row.ltp.toFixed(2) : '');
            if (topBid) manualLimitPrice[symbol] = topBid;
            // Clear throttle so first refresh fires immediately
            delete _limitRefreshTs[symbol];
            lpRow.innerHTML = `
                <span class="lp-label">Limit ₹</span>
                <input type="number" class="trade-qty-input limit-price-input"
                    id="lp-${symbol}" data-symbol="${symbol}"
                    value="${manualLimitPrice[symbol] || ''}"
                    step="0.05" placeholder="Price"
                    oninput="manualLimitPrice['${symbol}']=this.value; this.dataset.userEdited=1"
                />
                <button class="lp-bid-btn" onclick="setLimitToTopBid('${symbol}')" title="Use top bid">= Bid</button>
                <button class="lp-ask-btn" onclick="setLimitToTopAsk('${symbol}')" title="Use top ask">= Ask</button>
            `;
            ctrl.appendChild(lpRow);
        }
        lpRow.style.display = 'flex';
        // Auto-fetch fresh depth
        refreshDepth(symbol);
    } else {
        if (lpRow) lpRow.style.display = 'none';
    }
}

let _lastCashFetch = 0;
// refreshLiquidcaseCash kept as alias — called from buy/sell modal code
function refreshLiquidcaseCash() { refreshFundsCards(); }

let _lastFundsFetch = 0;
function refreshFundsCards() {
    // Throttle to once per 30s — keeps Available Cash & Margin in sync with
    // portfolio activity without hammering the Zerodha margins API.
    // After a trade the buy/sell handler resets _lastFundsFetch = 0 to force
    // an immediate refresh regardless of the throttle.
    const now = Date.now();
    if (now - _lastFundsFetch < 30000) return;
    _lastFundsFetch = now;

    fetch('/api/cash-balance', { cache: 'no-store' })
        .then(r => r.json())
        .then(data => {
            if (data.error) return;   // keep existing values on error

            const fmt = (v) => '₹' + (v || 0).toLocaleString('en-IN',
                {minimumFractionDigits: 0, maximumFractionDigits: 0});

            // Available Cash card — equity.available.cash (net opening balance)
            const cashEl = document.getElementById('avail-cash-val');
            if (cashEl) {
                cashEl.textContent = fmt(data.available_cash);
                cashEl.title = data.stale ? 'Cached' : 'Net opening balance from Kite';
            }

            // Available Margin card — collateral + live_balance (matches kite.zerodha.com/funds)
            const marginEl = document.getElementById('avail-margin-val');
            const breakdownEl = document.getElementById('avail-margin-breakdown');
            if (marginEl) {
                const margin = data.available_margin ?? data.available_cash ?? 0;
                marginEl.textContent = fmt(margin);
                marginEl.title = data.stale ? 'Cached' : 'Total Collateral − Used Margin + Available Cash';
            }
            if (breakdownEl) {
                const col  = data.collateral   ?? 0;
                const used = data.utilised      ?? 0;
                const cash = data.available_cash ?? 0;
                if (col > 0 || used > 0) {
                    breakdownEl.textContent = `Collateral ₹${col.toLocaleString('en-IN',{maximumFractionDigits:0})} · Used ₹${used.toLocaleString('en-IN',{maximumFractionDigits:0})}`;
                } else {
                    breakdownEl.textContent = '';
                }
            }


        })
        .catch(() => { /* keep existing values on network error */ });
}

function onAnytimeToggleChange(checkbox) {
    const buyTimeSelect = document.getElementById('bot-buy-time');
    if (buyTimeSelect) {
        buyTimeSelect.disabled = checkbox.checked;
        buyTimeSelect.style.opacity = checkbox.checked ? '0.45' : '1';
    }
}

function setDefaultOrderType(type, updateInput = true) {
    document.querySelectorAll('.ot-btn').forEach(b => b.classList.remove('active'));
    const btn = document.getElementById('ot-' + type.toLowerCase());
    if (btn) btn.classList.add('active');
    if (updateInput) {
        const inp = document.getElementById('bot-default-order-type');
        if (inp) inp.value = type;
    }
}

function syncPortfolio() {
    const syncBtn = document.querySelector('[onclick="syncPortfolio()"]');
    if (syncBtn) { syncBtn.disabled = true; syncBtn.textContent = '⟳ Syncing…'; }

    fetch('/api/portfolio/sync', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.error) {
                showToast(`Sync failed: ${data.error}`, 'error');
            } else {
                // Refresh both portfolio panel AND market monitor (both use holding data)
                loadPortfolio();
                loadMarketData();
                showToast('Portfolio synced', 'success');
            }
        })
        .catch(error => {
            console.error('Error syncing portfolio:', error);
            showToast('Failed to sync portfolio', 'error');
        })
        .finally(() => {
            if (syncBtn) { syncBtn.disabled = false; syncBtn.textContent = '⟳ SYNC PORTFOLIO'; }
        });
}

// Track known sell order_ids to avoid re-processing
let knownSellOrderIds = new Set();
let sellCheckInitialized = false;

function checkForSellAndSync() {
    // Skip if no override is active — nothing to reset
    if (!hasActiveOverride) return;
    
    fetch('/api/transactions')
        .then(response => response.json())
        .then(transactions => {
            if (!transactions || transactions.length === 0) return;
            
            // On first call, seed known IDs from existing transactions
            // so pre-existing sells don't trigger a false reset
            if (!sellCheckInitialized) {
                transactions.forEach(tx => {
                    if (tx.type === 'SELL' && tx.symbol !== 'LIQUIDCASE') {
                        knownSellOrderIds.add(tx.order_id);
                    }
                });
                sellCheckInitialized = true;
                return;
            }
            
            // Scan top 5 transactions for new ETF sells (handles swap ordering)
            const toCheck = transactions.slice(0, 5);
            for (const tx of toCheck) {
                if (tx.type === 'SELL' && 
                    tx.symbol !== 'LIQUIDCASE' && 
                    !knownSellOrderIds.has(tx.order_id)) {
                    knownSellOrderIds.add(tx.order_id);
                    console.log(`🔍 New ETF sell detected: ${tx.symbol} — reloading config`);
                    // Backend already reset settings.json; just reload to reflect
                    loadConfig();
                    break;
                }
            }
        })
        .catch(() => {});
}

function resetToDefaults() {
    if (savedDefaultProfit === null) return;
    
    const profitSelect = document.getElementById('profit-select');
    const testQuantityInput = document.getElementById('test-quantity');
    const currentProfit = profitSelect ? parseFloat(profitSelect.value) : savedDefaultProfit;
    const currentMaxQty = testQuantityInput ? parseInt(testQuantityInput.value) : savedDefaultMaxQty;
    
    if (currentProfit === savedDefaultProfit && currentMaxQty === savedDefaultMaxQty) {
        hasActiveOverride = false;
        return;
    }
    
    console.log(`🔄 Resetting: profit ${currentProfit}%→${savedDefaultProfit}%, qty ${currentMaxQty}→${savedDefaultMaxQty}`);
    
    fetch('/api/settings/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            profit_target_pct: savedDefaultProfit,
            test_quantity: savedDefaultMaxQty
        })
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === 'success') {
            if (profitSelect) profitSelect.value = savedDefaultProfit.toFixed(2);
            if (testQuantityInput) testQuantityInput.value = savedDefaultMaxQty;
            activeProfitTarget = savedDefaultProfit;
            activeMaxQty = savedDefaultMaxQty;
            currentProfitTarget = savedDefaultProfit;
            hasActiveOverride = false;
            showToast(`🔄 Reset to defaults: ${savedDefaultProfit}% profit target`, 'info');
        }
    })
    .catch(error => console.error('Reset failed:', error));
}

// Manual exit position
function manualExit(symbol, maxQty) {
    const defaultOT2    = document.getElementById('bot-default-order-type')?.value || 'MARKET';
    const exitOrderType = manualOrderType[symbol] || defaultOT2;
    let exitLimitPrice  = null;
    if (exitOrderType === 'LIMIT') {
        const lpInp = document.getElementById(`lp-${symbol}`);
        exitLimitPrice = lpInp ? parseFloat(lpInp.value) || null
                               : (parseFloat(manualLimitPrice[symbol]) || null);
        if (!exitLimitPrice) {
            const row = marketDataCache?.find(r => r.symbol === symbol);
            exitLimitPrice = row?.top_bid || row?.ltp || null;
        }
        if (!exitLimitPrice) {
            showToast(`Enter a limit price for ${symbol} before placing a LIMIT exit`, 'error');
            return;
        }
    }
    // Get quantity from input field
    const input = document.querySelector(`.exit-input[data-symbol="${symbol}"]`);
    let quantity = parseInt(input.value) || maxQty;
    
    // Validate quantity
    if (quantity <= 0) {
        showToast('Invalid quantity', 'error');
        return;
    }
    if (quantity > maxQty) {
        quantity = maxQty;
        input.value = quantity;
    }
    
    // Confirm before selling
    const message = `Confirm manual exit:\nSell ${quantity} units of ${symbol}?`;
    if (!window.confirm(message)) {
        return;
    }
    
    // Disable button to prevent double-clicks
    const exitBtn = document.querySelector(`.exit-btn[onclick*="${symbol}"]`);
    if (exitBtn) {
        exitBtn.disabled = true;
        exitBtn.style.opacity = '0.5';
        exitBtn.style.cursor = 'not-allowed';
    }
    
    // Show loading toast
    showToast(`Placing exit order for ${quantity} ${symbol}...`, 'info');
    
    // Send exit request
    const exitLabel = exitOrderType === 'LIMIT'
        ? `LIMIT @ ₹${Number(exitLimitPrice).toFixed(2)}`
        : 'MARKET';
    showToast(`Placing ${exitLabel} exit: ${quantity} × ${symbol}`, 'info');

    fetch('/api/manual-exit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            symbol,
            quantity,
            order_type:  exitOrderType,
            limit_price: exitLimitPrice
        })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showToast(data.message || `Exit order placed: ${quantity} ${symbol}`, 'success');
                
                // Clear stored quantity (will be updated from server on next refresh)
                delete manualTradeQty[`${symbol}_exit`];
                
                console.log(`💡 Exit complete - quantity will update from server on next refresh`);
                
                // Force immediate funds refresh so Available Cash/Margin reflects the trade
                _lastFundsFetch = 0;
                setTimeout(() => refreshFundsCards(), 1500);
                
                // Backend resets override after manual exit — reload config immediately
                if (data.settings_reset) {
                    setTimeout(() => { loadConfig(); }, 500);
                }
                
                // Refresh data (will re-enable button via table rebuild)
                setTimeout(() => {
                    syncPortfolio();
                    loadMarketData();
                }, 1000);
            } else {
                const exitErrMsg = data.error || 'Failed to place exit order';
                showToast((data.zerodha_error ? '🔴 Zerodha: ' : '❌ ') + exitErrMsg, 'error');
                // Re-enable button on error
                if (exitBtn) {
                    exitBtn.disabled = false;
                    exitBtn.style.opacity = '1';
                    exitBtn.style.cursor = 'pointer';
                }
            }
        })
        .catch(error => {
            console.error('Error placing manual exit:', error);
            showToast('Failed to place exit order', 'error');
            // Re-enable button on error
            if (exitBtn) {
                exitBtn.disabled = false;
                exitBtn.style.opacity = '1';
                exitBtn.style.cursor = 'pointer';
            }
        });
}

// Manual buy position
function manualBuy(symbol) {
    const defaultOT = document.getElementById('bot-default-order-type')?.value || 'MARKET';
    const orderType = manualOrderType[symbol] || defaultOT;
    // Get quantity from input field
    const input = document.querySelector(`.buy-input[data-symbol="${symbol}"]`);
    let quantity = parseInt(input.value) || 1;
    
    // Validate quantity
    if (quantity <= 0) {
        showToast('Invalid quantity', 'error');
        return;
    }
    
    // Confirm before buying
    const message = `Confirm manual buy:\nBuy ${quantity} units of ${symbol}?`;
    if (!window.confirm(message)) {
        return;
    }
    
    // Disable button to prevent double-clicks
    const buyBtn = document.querySelector(`.buy-btn[onclick*="${symbol}"]`);
    if (buyBtn) {
        buyBtn.disabled = true;
        buyBtn.style.opacity = '0.5';
        buyBtn.style.cursor = 'not-allowed';
    }
    
    // Show loading toast
    showToast(`Placing buy order for ${quantity} ${symbol}...`, 'info');
    
    // Resolve order type and limit price for this row
    const defaultOT2   = document.getElementById('bot-default-order-type')?.value || 'MARKET';
    const rowOrderType = manualOrderType[symbol] || defaultOT2;
    let rowLimitPrice  = null;
    if (rowOrderType === 'LIMIT') {
        // Prefer the editable input value, fallback to stored/top-bid
        const lpInp = document.getElementById(`lp-${symbol}`);
        rowLimitPrice = lpInp ? parseFloat(lpInp.value) || null
                              : (parseFloat(manualLimitPrice[symbol]) || null);
        if (!rowLimitPrice) {
            const row = marketDataCache?.find(r => r.symbol === symbol);
            rowLimitPrice = row?.top_bid || row?.ltp || null;
        }
        if (!rowLimitPrice) {
            showToast(`Enter a limit price for ${symbol} before placing a LIMIT order`, 'error');
            return;
        }
    }
    const orderLabel = rowOrderType === 'LIMIT'
        ? `LIMIT @ ₹${Number(rowLimitPrice).toFixed(2)}`
        : 'MARKET';
    showToast(`Placing ${orderLabel} buy: ${quantity} × ${symbol}`, 'info');

    // Send buy request
    fetch('/api/manual-buy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            symbol,
            quantity,
            order_type:  rowOrderType,
            limit_price: rowLimitPrice
        })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showToast(data.message || `Buy order placed: ${quantity} ${symbol}`, 'success');
                
                // Reset input field to 1 (default for next buy)
                input.value = '1';
                console.log(`💡 Buy input reset to 1 for next trade`);
                
                // Clear stored quantity
                delete manualTradeQty[`${symbol}_buy`];
                
                // Force immediate funds refresh so Available Cash/Margin reflects the trade
                _lastFundsFetch = 0;
                setTimeout(() => refreshFundsCards(), 1500);
                
                // Refresh data (will re-enable button via table rebuild)
                setTimeout(() => {
                    syncPortfolio();
                    loadMarketData();
                }, 1000);
            } else {
                const errMsg = data.error || 'Failed to place buy order';
                const isKite = data.zerodha_error;
                showToast((isKite ? '🔴 Zerodha: ' : '❌ ') + errMsg, 'error');
                // Re-enable button on error
                if (buyBtn) {
                    buyBtn.disabled = false;
                    buyBtn.style.opacity = '1';
                    buyBtn.style.cursor = 'pointer';
                }
            }
        })
        .catch(error => {
            console.error('Error placing manual buy:', error);
            showToast('Failed to place buy order', 'error');
            // Re-enable button on error
            if (buyBtn) {
                buyBtn.disabled = false;
                buyBtn.style.opacity = '1';
                buyBtn.style.cursor = 'pointer';
            }
        });
}

// Update bot button states
function updateBotButtons() {
    const startBtn = document.getElementById('btn-start');
    const pauseBtn = document.getElementById('btn-pause');
    
    if (botRunning) {
        startBtn.disabled = true;
        pauseBtn.disabled = false;
    } else {
        startBtn.disabled = false;
        pauseBtn.disabled = true;
    }
}

// Start auto-refresh for display data (1 second for real-time feel)
function startAutoRefresh() {
    if (refreshInterval) {
        clearInterval(refreshInterval);
    }

    // 15s — reads from WebSocket in-memory cache; prices stay fresh via WS ticks.
    // Increased from 5s to reduce OMS request rate and avoid 403 / re-auth loops.
    refreshInterval = setInterval(() => {
        loadStatus();
        loadPortfolio();
        loadPositions();       // uses server-side 10-second cache; safe to call here
        loadMarketData();
    }, 15000);

    // 10s — indices and transactions don't need sub-second refresh
    if (transactionInterval) {
        clearInterval(transactionInterval);
    }
    transactionInterval = setInterval(() => {
        loadIndices();
        loadTransactions();
        checkForSellAndSync();
    }, 10000);

    // 30s — heavy operations: Zerodha sync, config reload, logs, funds/margin
    if (syncInterval) {
        clearInterval(syncInterval);
    }
    syncInterval = setInterval(() => {
        autoSyncPortfolio();
        loadConfig();
        loadLogs();
        refreshFundsCards();
    }, 30000);
}

// Auto-sync portfolio (silent, no toast)
function autoSyncPortfolio() {
    fetch('/api/portfolio/sync', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            console.log('📊 Portfolio auto-synced with Zerodha');
            // Immediately refresh display to show updated data
            loadPortfolio();
        })
        .catch(error => {
            console.error('Auto-sync error:', error);
        });
}

// Utility functions
function formatNumber(num) {
    return num.toLocaleString('en-IN', { 
        minimumFractionDigits: 2, 
        maximumFractionDigits: 2 
    });
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Holdings Modal Functions
function openHoldingsModal() {
    const modal = document.getElementById('holdings-modal');
    modal.style.display = 'flex';

    // Show loading state while fresh data is fetched
    const modalBody = document.getElementById('holdings-modal-body');
    if (modalBody && !modalBody.querySelector('tr[data-symbol]')) {
        modalBody.innerHTML = '<tr><td colspan="8" class="empty-state" style="color:var(--text-secondary)">⏳ Loading…</td></tr>';
    }

    loadPortfolio();
}

function closeHoldingsModal() {
    const modal = document.getElementById('holdings-modal');
    modal.style.display = 'none';
}

// Toggle quick settings
function toggleQuickSettings() {
    const content = document.getElementById('settings-content');
    const toggle = document.getElementById('settings-toggle');
    
    if (content.style.display === 'none') {
        content.style.display = 'block';
        toggle.textContent = '▲';
    } else {
        content.style.display = 'none';
        toggle.textContent = '▼';
    }
}

// Toggle Bot Strategy Settings in sidebar
function toggleBotSettings() {
    const content = document.getElementById('bot-settings-content');
    const toggle = document.getElementById('bot-settings-toggle');
    
    if (content.style.display === 'none') {
        content.style.display = 'block';
        toggle.textContent = '▲';
    } else {
        content.style.display = 'none';
        toggle.textContent = '▼';
    }
}

function saveBotSettings() {
    const botProfitTarget    = document.getElementById('bot-profit-target');
    const botMaxQty          = document.getElementById('bot-max-qty');
    const botSlots           = document.getElementById('bot-slots');
    const botMaxCashPerStock = document.getElementById('bot-max-cash-per-stock');
    const botMaxCashPerTx    = document.getElementById('bot-max-cash-per-tx');
    const botMinPriceDrop    = document.getElementById('bot-min-price-drop');
    const botBuyTime         = document.getElementById('bot-buy-time');
    const botCashReserve     = document.getElementById('bot-cash-reserve');

    if (!botProfitTarget || !botMaxQty || !botSlots) {
        showToast('Settings inputs not found', 'error');
        return;
    }

    const profitValue        = parseFloat(botProfitTarget.value);
    const maxQtyValue        = parseInt(botMaxQty.value) || 0;
    const slotsValue         = parseInt(botSlots.value);
    const maxCashPerStock    = parseInt(botMaxCashPerStock?.value) || 0;
    const maxCashPerTx       = parseInt(botMaxCashPerTx?.value) || 0;
    const minPriceDrop       = parseFloat(botMinPriceDrop?.value) || 1.0;
    const anytimeToggle      = document.getElementById('bot-anytime-toggle');
    const buyTime            = (anytimeToggle?.checked) ? 'anytime' : (botBuyTime?.value || '15:15');
    const cashReserve        = parseFloat(botCashReserve?.value) || 0;

    if (isNaN(profitValue) || profitValue < 0.5 || profitValue > 10) {
        showToast('Profit Target must be between 0.5% and 10%', 'error');
        return;
    }
    if (maxQtyValue < 0) {
        showToast('Max Qty cannot be negative', 'error');
        return;
    }
    if (isNaN(slotsValue) || slotsValue < 1 || slotsValue > 5) {
        showToast('Slots must be between 1 and 5', 'error');
        return;
    }
    if (maxCashPerStock < 0) {
        showToast('Max Cash per Stock cannot be negative', 'error');
        return;
    }
    if (maxCashPerTx < 0) {
        showToast('Max Cash per Transaction cannot be negative', 'error');
        return;
    }
    if (maxCashPerTx > 0 && maxCashPerStock > 0 && maxCashPerTx > maxCashPerStock) {
        showToast('Max Cash per Transaction cannot exceed Max Cash per Stock', 'error');
        return;
    }
    if (isNaN(minPriceDrop) || minPriceDrop < 0 || minPriceDrop > 20) {
        showToast('Min Price Drop must be between 0% and 20%', 'error');
        return;
    }

    // Save both active AND default values (server is source of truth)
    const updates = {
        profit_target_pct: profitValue,
        default_profit_target_pct: profitValue,
        test_quantity: maxQtyValue,
        default_test_quantity: maxQtyValue,
        slots_count: slotsValue,
        max_cash_per_stock: maxCashPerStock,
        max_cash_per_transaction: maxCashPerTx,
        min_price_drop_pct:  minPriceDrop,
        buy_execution_time:  buyTime,
        default_order_type:  document.getElementById('bot-default-order-type')?.value || 'MARKET',
        cash_reserve:        cashReserve,
    };
    
    fetch('/api/settings/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updates)
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === 'success') {
            savedDefaultProfit = profitValue;
            savedDefaultMaxQty = maxQtyValue;
            activeProfitTarget = profitValue;
            activeMaxQty = maxQtyValue;
            currentProfitTarget = profitValue;
            hasActiveOverride = false;
            
            const profitSelect = document.getElementById('profit-select');
            const testQuantityInput = document.getElementById('test-quantity');
            if (profitSelect) profitSelect.value = profitValue.toFixed(2);
            if (testQuantityInput) testQuantityInput.value = maxQtyValue;
            
            const qtyDisplay  = maxQtyValue === 0 ? 'unlimited' : maxQtyValue;
            const timeDisplay = buyTime === 'anytime' ? 'Anytime (no gate)' : buyTime;
            const cashStockDisplay = maxCashPerStock === 0 ? 'unlimited' : '₹' + maxCashPerStock.toLocaleString('en-IN');
            const cashTxDisplay    = maxCashPerTx    === 0 ? 'unlimited' : '₹' + maxCashPerTx.toLocaleString('en-IN');
            showToast(`✅ Saved: ${profitValue}% profit · ${qtyDisplay} qty · ${slotsValue} slots · ₹/stock ${cashStockDisplay} · ₹/tx ${cashTxDisplay} · exec: ${timeDisplay}`, 'success');
            
            setTimeout(() => { loadPortfolio(); loadMarketData(); }, 500);
        } else {
            showToast(data.error || 'Failed to save settings', 'error');
        }
    })
    .catch(error => {
        console.error('Error saving settings:', error);
        showToast('Failed to save settings', 'error');
    });
}

// Toast Notification System
function showToast(message, type = 'success') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    
    const icons = {
        success: '✓',
        error: '✕',
        warning: '⚠',
        info: 'ℹ'
    };
    
    toast.innerHTML = `
        <span class="toast-icon">${icons[type] || '●'}</span>
        <span class="toast-message">${message}</span>
        <button class="toast-close" onclick="this.parentElement.remove()">×</button>
    `;
    
    container.appendChild(toast);
    
    // Auto-remove after 4 seconds
    setTimeout(() => {
        toast.classList.add('hiding');
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

// Handle settings changes
document.addEventListener('DOMContentLoaded', function() {
    // Mode change handler
    const modeSelect = document.getElementById('mode-select');
    if (modeSelect) {
        modeSelect.addEventListener('change', function() {
            const newValue = this.value;
            if (newValue === 'live') {
                if (!confirm('Switch to LIVE TRADING?\n\nReal money will be used!')) {
                    this.value = 'dry';
                    return;
                }
            }
            updateSettings('trading_mode', newValue);
        });
    }
    
    // Slots change handler
    const slotsSelect = document.getElementById('slots-select');
    if (slotsSelect) {
        slotsSelect.addEventListener('change', function() {
            const newValue = parseInt(this.value);
            updateSettings('slots_count', newValue);
        });
    }
    
    const profitSelect = document.getElementById('profit-select');
    if (profitSelect) {
        profitSelect.addEventListener('change', function() {
            let newValue = parseFloat(this.value);
            if (isNaN(newValue) || newValue < 0.5) { newValue = 0.5; }
            else if (newValue > 10) { newValue = 10; }
            this.value = newValue.toFixed(2);
            
            updateSettings('profit_target_pct', newValue);
            currentProfitTarget = newValue;
            activeProfitTarget = newValue;
            hasActiveOverride = (newValue !== savedDefaultProfit) || (activeMaxQty !== savedDefaultMaxQty);
            
            if (newValue !== savedDefaultProfit) {
                showToast(`⚡ Temp: ${newValue}% (resets to ${savedDefaultProfit}% after sell)`, 'warning');
            } else {
                showToast(`✅ Profit Target: ${newValue}%`, 'success');
            }
        });
        
        profitSelect.addEventListener('blur', function() {
            let v = parseFloat(this.value);
            if (isNaN(v) || v < 0.5) v = 0.5;
            else if (v > 10) v = 10;
            this.value = v.toFixed(2);
        });
    }
    
    const testQuantityInput = document.getElementById('test-quantity');
    if (testQuantityInput) {
        testQuantityInput.addEventListener('change', function() {
            const newValue = Math.max(0, parseInt(this.value) || 0);
            this.value = newValue;
            
            updateSettings('test_quantity', newValue);
            activeMaxQty = newValue;
            hasActiveOverride = (activeProfitTarget !== savedDefaultProfit) || (newValue !== savedDefaultMaxQty);
            
            const displayQty = newValue === 0 ? 'UNLIMITED' : newValue + ' units';
            const defaultDisplay = savedDefaultMaxQty === 0 ? 'UNLIMITED' : savedDefaultMaxQty + ' units';
            
            if (newValue !== savedDefaultMaxQty) {
                showToast(`⚡ Temp: ${displayQty} (resets to ${defaultDisplay} after sell)`, 'warning');
            } else {
                showToast(`✅ Max Qty: ${displayQty}`, 'success');
            }
        });
    }
    
    const resetManualBtn = document.getElementById('reset-manual-settings');
    if (resetManualBtn) {
        resetManualBtn.addEventListener('click', function() {
            const defaultQtyDisplay = savedDefaultMaxQty === 0 ? 'UNLIMITED' : savedDefaultMaxQty + ' units';
            if (!confirm(`Reset to saved defaults?\n\nProfit Target: ${savedDefaultProfit}%\nMax Qty: ${defaultQtyDisplay}`)) {
                return;
            }
            resetToDefaults();
        });
    }
});

// Load Transaction Log
function loadTransactions() {
    fetch('/api/transactions')
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            return response.json();
        })
        .then(data => {
            updateTransactionsTable(data, null);
        })
        .catch(error => {
            console.error('Error loading transactions:', error);
            updateTransactionsTable([], error.message);
        });
}

// Update transactions table
function updateTransactionsTable(transactions, fetchError) {
    const tbody = document.getElementById('transaction-body');
    
    if (fetchError) {
        tbody.innerHTML = `<tr><td colspan="7" class="empty-state" style="color:var(--danger)">⚠️ Could not load transactions (${fetchError})</td></tr>`;
        return;
    }
    if (!transactions || transactions.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty-state">No transactions today</td></tr>';
        return;
    }
    
    tbody.innerHTML = transactions.map(t => {
        // Format time
        const date = new Date(t.time);
        const timeStr = date.toLocaleTimeString('en-IN', { 
            hour: '2-digit', 
            minute: '2-digit'
        });
        
        // Transaction type styling
        const typeClass = t.type === 'BUY' ? 'tx-buy' : 'tx-sell';
        
        // Status styling
        let statusClass = 'tx-status-pending';
        let statusText = t.status;
        if (t.status === 'COMPLETE') {
            statusClass = 'tx-status-complete';
            statusText = '✓ Complete';
        } else if (t.status === 'REJECTED' || t.status === 'CANCELLED') {
            statusClass = 'tx-status-rejected';
            statusText = '✗ ' + t.status;
        } else if (t.status === 'OPEN') {
            statusClass = 'tx-status-open';
            statusText = '⏳ Open';
        }
        
        return `
            <tr>
                <td>${timeStr}</td>
                <td><span class="tx-type ${typeClass}">${t.type}</span></td>
                <td><strong>${t.symbol}</strong></td>
                <td>${t.filled_quantity || t.quantity}</td>
                <td>₹${t.price.toFixed(2)}</td>
                <td><span class="tx-status ${statusClass}">${statusText}</span></td>
                <td>₹${t.value.toFixed(2)}</td>
            </tr>
        `;
    }).join('');
}

function updateSettings(key, value) {
    fetch('/api/settings/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [key]: value })
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === 'success') {
            if (key === 'trading_mode') {
                const isLive = (value === 'live');
                const modeBadge = document.getElementById('mode-badge');
                if (modeBadge) {
                    modeBadge.textContent = isLive ? '🔴 LIVE TRADING' : '🟡 DRY RUN';
                    modeBadge.style.backgroundColor = isLive ? '#ef4444' : '#f59e0b';
                    modeBadge.style.color = isLive ? '#ffffff' : '';
                    modeBadge.style.borderColor = isLive ? '#dc2626' : '';
                }
                showToast(isLive ? '⚠️ LIVE MODE activated!' : 'Switched to DRY RUN', isLive ? 'warning' : 'success');
            } else {
                const messages = {
                    'slots_count': `Slots updated to ${value}`,
                    'profit_target_pct': `Profit target set to ${value}%`
                };
                showToast(messages[key] || 'Settings updated', 'success');
            }
        } else {
            showToast('Failed to update: ' + (data.error || 'Unknown error'), 'error');
            loadConfig();
        }
    })
    .catch(error => {
        console.error('Error updating settings:', error);
        showToast('Failed to update settings', 'error');
        loadConfig();
    });
}

// ═══════════════════════════════════════════════════════════════════
// INTRADAY ENGINE — Dashboard JS
// ═══════════════════════════════════════════════════════════════════

let intradayRefreshInterval = null;

// ── Intraday alert state ──────────────────────────────────────────────────────
// Tracks how many trades the dashboard has already seen so we only alert on
// truly new events (not on every 2-sec poll, and not on initial page load).
let intradayLastTradeCount = -1;          // -1 = baseline not yet captured
let intradayAudioCtx       = null;        // lazily created on first user gesture
let intradayAlertsEnabled  = true;        // user-toggleable mute

function intradayPlayBeep(kind) {
    if (!intradayAlertsEnabled) return;
    try {
        if (!intradayAudioCtx) {
            const Ctx = window.AudioContext || window.webkitAudioContext;
            if (!Ctx) return;
            intradayAudioCtx = new Ctx();
        }
        const ctx = intradayAudioCtx;
        if (ctx.state === 'suspended') ctx.resume();

        // Different pitch / pattern per event kind
        const presets = {
            'entry':    { freq: 660, dur: 0.18, type: 'sine',     gain: 0.18 }, // first attempt
            'add':      { freq: 520, dur: 0.14, type: 'triangle', gain: 0.16 }, // DCA add
            'target':   { freq: 880, dur: 0.45, type: 'sine',     gain: 0.22 }, // 1% target hit
            'squareoff':{ freq: 330, dur: 0.30, type: 'square',   gain: 0.18 }, // 3:15 / manual close
        };
        const p = presets[kind] || presets['entry'];

        const osc  = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type            = p.type;
        osc.frequency.value = p.freq;
        gain.gain.value     = p.gain;
        osc.connect(gain).connect(ctx.destination);
        const now = ctx.currentTime;
        gain.gain.setValueAtTime(p.gain, now);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + p.dur);
        osc.start(now);
        osc.stop(now + p.dur + 0.02);

        // For 'target' play a quick second chirp for emphasis
        if (kind === 'target') {
            setTimeout(() => intradayPlayBeep('entry'), 220);
        }
    } catch (e) { /* silent */ }
}

function intradayFlashPanel(cssClass) {
    const tab = document.getElementById('tab-intraday');
    if (!tab) return;
    tab.classList.remove('intraday-flash-success', 'intraday-flash-warn', 'intraday-flash-loss');
    // force reflow so animation restarts
    void tab.offsetWidth;
    tab.classList.add(cssClass);
    setTimeout(() => tab.classList.remove(cssClass), 1800);
}

function intradayHandleNewTrades(tradeLog) {
    if (!Array.isArray(tradeLog)) return;

    // First sighting after page load → just record baseline, no alerts.
    if (intradayLastTradeCount < 0) {
        intradayLastTradeCount = tradeLog.length;
        return;
    }
    if (tradeLog.length <= intradayLastTradeCount) return;

    const newOnes = tradeLog.slice(intradayLastTradeCount);
    intradayLastTradeCount = tradeLog.length;

    newOnes.forEach(t => {
        const isExit = t.pnl_amt != null;   // engine fills pnl only on square-off
        if (isExit) {
            const profit = t.pnl_amt >= 0;
            const pct    = t.pnl_pct != null ? t.pnl_pct.toFixed(2) + '%' : '';
            const amt    = t.pnl_amt != null ? '₹' + t.pnl_amt.toFixed(2) : '';
            if (profit && /target/i.test(t.reason || '')) {
                showToast(`🎯 1% TARGET HIT — ${t.side} squared off · ${amt} (${pct})`, 'success');
                intradayPlayBeep('target');
                intradayFlashPanel('intraday-flash-success');
            } else if (profit) {
                showToast(`✅ ${t.side} squared off · ${amt} (${pct}) — ${t.reason || ''}`, 'success');
                intradayPlayBeep('squareoff');
                intradayFlashPanel('intraday-flash-success');
            } else {
                showToast(`🔻 ${t.side} squared off at LOSS · ${amt} (${pct}) — ${t.reason || ''}`, 'warning');
                intradayPlayBeep('squareoff');
                intradayFlashPanel('intraday-flash-loss');
            }
        } else {
            // Entry / DCA add
            const isFirst = (t.attempt === 1);
            const label   = isFirst ? `${t.side} OPEN` : `${t.side} DCA add #${t.attempt}`;
            showToast(
                `⚡ ${label} — ${t.action} ${t.qty} × ${t.symbol || 'ETF'} @ ₹${t.price?.toFixed(2)} (W%R ${t.wr})`,
                'info'
            );
            intradayPlayBeep(isFirst ? 'entry' : 'add');
            intradayFlashPanel('intraday-flash-warn');
        }
    });
}

// ── Daily P&L summary ─────────────────────────────────────────────
// Walks the trade_log and aggregates every square-off entry (closed cycle).
// Square-off entries carry numeric `pnl_amt` and `pnl_pct` from the engine;
// open / DCA-add entries do not, so they're ignored here.
function intradayUpdatePnlSummary(tradeLog) {
    let total = 0, wins = 0, losses = 0, winSum = 0, lossSum = 0;
    let best = null, worst = null;
    let longCycles = 0, longPnl = 0, shortCycles = 0, shortPnl = 0;

    tradeLog.forEach(t => {
        const isClose = t.type === 'SQUAREOFF' || t.action === 'SELL_ALL' || t.action === 'COVER_ALL';
        if (!isClose) return;
        const amt = Number(t.pnl_amt);
        if (!Number.isFinite(amt)) return;

        total += amt;
        if (amt >= 0) { wins++;   winSum  += amt; }
        else          { losses++; lossSum += amt; }
        if (best  === null || amt > best ) best  = amt;
        if (worst === null || amt < worst) worst = amt;

        if (t.side === 'SHORT') { shortCycles++; shortPnl += amt; }
        else                    { longCycles++;  longPnl  += amt; }
    });

    const cycles  = wins + losses;
    const winRate = cycles > 0 ? (wins / cycles * 100) : null;
    const fmt     = v => (v >= 0 ? '+₹' : '-₹') + Math.abs(v).toFixed(2);
    const cls     = v => v >= 0 ? 'pnl-positive' : 'pnl-negative';
    const setEl   = (id, txt, klass) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.textContent = txt;
        if (klass !== undefined) {
            el.classList.remove('pnl-positive','pnl-negative');
            if (klass) el.classList.add(klass);
        }
    };

    setEl('id-pnl-total',    fmt(total), cls(total));
    setEl('id-pnl-winrate',  winRate === null ? '—' : winRate.toFixed(1) + '%');
    setEl('id-pnl-cycles',   String(cycles));
    setEl('id-pnl-wl',       wins + ' / ' + losses);
    setEl('id-pnl-avg-win',  wins   > 0 ? fmt(winSum  / wins)   : '—');
    setEl('id-pnl-avg-loss', losses > 0 ? fmt(lossSum / losses) : '—');
    setEl('id-pnl-best',     best  === null ? '—' : fmt(best));
    setEl('id-pnl-worst',    worst === null ? '—' : fmt(worst));
    setEl('id-pnl-long',     longCycles  + ' · ' + fmt(longPnl),  cls(longPnl));
    setEl('id-pnl-short',    shortCycles + ' · ' + fmt(shortPnl), cls(shortPnl));
}

// ── Dip Accumulator functions ─────────────────────────────────────────────────────
function _bnhSet(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
}

function startBnHBot() {
    fetch('/api/intraday/start', { method: 'POST' })
        .then(r => r.json())
        .then(d => {
            if (d.success) {
                showToast('📈 Dip Accumulator & Harvester bot STARTED — monitoring BnH symbols daily W%R', 'success');
                updateBnHUI(true);
                startBnHRefresh();
            } else {
                showToast('❌ ' + (d.error || 'Could not start Dip Accumulator & Harvester bot'), 'error');
            }
        })
        .catch(() => showToast('❌ Network error starting Dip Accumulator & Harvester bot', 'error'));
}

function stopBnHBot() {
    fetch('/api/intraday/stop', { method: 'POST' })
        .then(r => r.json())
        .then(d => {
            if (d.success) {
                showToast('⏹ Dip Accumulator & Harvester bot STOPPED', 'warning');
                updateBnHUI(false);
            } else {
                showToast('❌ ' + (d.error || 'Could not stop Dip Accumulator & Harvester bot'), 'error');
            }
        })
        .catch(() => showToast('❌ Network error', 'error'));
}

function updateBnHUI(running) {
    // Buttons exist in both Controls tab and (status badge) in BnH tab
    ['btn-bnh-start'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = running;
    });
    ['btn-bnh-stop'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = !running;
    });
    const dot   = document.getElementById('bnh-dot');
    const text  = document.getElementById('bnh-status-text');
    const badge = document.getElementById('bnh-status-badge');
    if (dot)   dot.className    = running ? 'status-dot dot-live' : 'status-dot dot-stopped';
    if (text)  text.textContent = running ? 'RUNNING' : 'Stopped';
    if (badge) badge.className  = 'intraday-status-badge ' + (running ? 'badge-live' : '');
}

function saveBnHSettings() {
    const etf     = parseFloat(document.getElementById('bnh-max-cash-etf')?.value) || 0;
    const txn     = parseFloat(document.getElementById('bnh-max-cash-txn')?.value) || 0;
    const pctEl   = document.getElementById('bnh-partial-profit-pct');
    const pct     = pctEl ? parseFloat(pctEl.value) : NaN;

    if (etf < 1000) { showToast('Max Cash / ETF must be at least ₹1,000', 'error'); return; }
    if (txn < 500)  { showToast('Max Cash / Transaction must be at least ₹500', 'error'); return; }
    if (txn > etf)  { showToast('Max Cash / Transaction cannot exceed Max Cash / ETF', 'error'); return; }
    if (pctEl && (isNaN(pct) || pct < 1 || pct > 50)) {
        showToast('Harvest Target must be between 1% and 50%', 'error'); return;
    }

    const payload = { bnh_max_cash_per_etf: etf, bnh_max_cash_per_transaction: txn };
    if (pctEl && !isNaN(pct)) payload.bnh_partial_profit_pct = pct;

    fetch('/api/bnh/save_settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
    })
    .then(r => r.json())
    .then(d => {
        if (d.success) {
            const pctSaved = d.bnh_partial_profit_pct ?? pct;
            // Update all hardcoded "5%" labels in the page to reflect the saved value
            ['bnh-subtitle-pct', 'bnh-ref-pct', 'bnh-kv-pct'].forEach(id => {
                const el = document.getElementById(id);
                if (el) el.textContent = pctSaved;
            });
            showToast(`✅ Saved — Max ETF ₹${etf.toLocaleString('en-IN')} · Max Txn ₹${txn.toLocaleString('en-IN')} · Harvest ${pctSaved}%`, 'success');
        } else {
            showToast('❌ Save failed: ' + (d.error || 'unknown error'), 'error');
        }
    })
    .catch(err => showToast('❌ Could not reach server — check connection', 'error'));
    // Optimistic UI update
    updateBnHRuleDisplay(etf, txn);
}

function updateBnHRuleDisplay(maxEtf, maxTxn) {
    const fmtINR = v => v != null ? '₹' + Number(v).toLocaleString('en-IN') : '—';
    _bnhSet('bnh-rule-max-etf', fmtINR(maxEtf));
    _bnhSet('bnh-rule-max-txn', fmtINR(maxTxn));
}

function loadBnHStatus() {
    fetch('/api/intraday/status')
        .then(r => r.json())
        .then(d => {
            if (!d.available) return;
            updateBnHUI(d.running);

            const fmt    = (v, dec=2) => v != null ? '₹' + Number(v).toFixed(dec) : '—';
            const fmtINR = v => v != null ? '₹' + Number(v).toLocaleString('en-IN', {maximumFractionDigits:0}) : '—';
            const fmtN   = (v, dec=2) => v != null ? Number(v).toFixed(dec) : '—';

            // ── Cache for Activity tab signal summary ─────────────────────────
            // Store the full multi-symbol status; keep single-symbol compat via .latest
            bnhLatestCache = d;
            updateSignalSummary();

            // ── Session summary panel ─────────────────────────────────────────
            _bnhSet('bnh-deployed-today', fmtINR(d.deployed_today));
            _bnhSet('bnh-total-deployed', fmtINR(d.total_deployed));
            _bnhSet('bnh-last-update',    new Date().toLocaleTimeString('en-IN', {hour:'2-digit',minute:'2-digit',second:'2-digit'}));
            _bnhSet('bnh-liquidcase-qty', d.liquidcase_qty != null ? d.liquidcase_qty : '—');
            _bnhSet('bnh-liquidcase-val', fmtINR(d.liquidcase_val));
            updateBnHRuleDisplay(d.max_cash_per_etf, d.max_cash_per_txn);

            const firstLat = d.latest || {};
            _bnhSet('bnh-order-type', firstLat.order_type || '—');

            // ── Signal bar — summarise all symbols ────────────────────────────
            const bar       = document.getElementById('bnh-signal-bar');
            const symStatus = d.symbols_status || {};
            const syms      = d.symbols || [];
            if (bar) {
                if (!d.running) {
                    bar.textContent = '⏸ Engine stopped — press START to begin monitoring';
                    bar.className   = 'intraday-signal-bar bar-neutral';
                } else if (!syms.length) {
                    bar.textContent = '⚠️ No symbols configured — add symbols in Manage Symbols';
                    bar.className   = 'intraday-signal-bar bar-neutral';
                } else {
                    const oversoldSyms = syms.filter(s => (symStatus[s] || {}).wr_state === 'OVERSOLD');
                    const boughtSyms   = syms.filter(s => (symStatus[s] || {}).bought_today === true);
                    if (oversoldSyms.length > 0) {
                        const boughtToday = oversoldSyms.filter(s => boughtSyms.includes(s));
                        const pendingBuy  = oversoldSyms.filter(s => !boughtSyms.includes(s));
                        const parts = [];
                        if (pendingBuy.length)  parts.push(`✅ ${pendingBuy.join(', ')} — BUY @ 3:15 PM`);
                        if (boughtToday.length) parts.push(`✅ Accumulated today: ${boughtToday.join(', ')}`);
                        bar.textContent = parts.join(' · ');
                        bar.className   = 'intraday-signal-bar bar-bull';
                    } else {
                        const wrVals = syms.map(s => {
                            const wr = (symStatus[s] || {}).wr;
                            return wr != null ? `${s} W%R ${Number(wr).toFixed(1)}` : null;
                        }).filter(Boolean);
                        bar.textContent = `😐 No signals — ${wrVals.join(' · ')} (need ≤ -60)`;
                        bar.className   = 'intraday-signal-bar bar-neutral';
                    }
                }
            }

            // ── Per-symbol live table ─────────────────────────────────────────
            const tbody = document.getElementById('bnh-live-body');
            if (tbody && syms.length > 0) {
                tbody.innerHTML = syms.map(sym => {
                    const s  = symStatus[sym] || {};
                    const h  = s.holdings   || {};
                    const wr = s.wr != null  ? Number(s.wr) : null;
                    const wrCls = wr != null && wr <= -60 ? 'wr-oversold' : '';
                    const pnlCls = h.pnl_amt != null ? (h.pnl_amt >= 0 ? 'pnl-positive' : 'pnl-negative') : '';
                    const stateCls = s.wr_state === 'OVERSOLD' ? 'pnl-positive' : '';
                    return `<tr>
                        <td><strong>${sym}</strong></td>
                        <td>${s.price != null ? '₹' + Number(s.price).toFixed(2) : '—'}</td>
                        <td class="${wrCls}">${wr != null ? wr.toFixed(1) : '—'}</td>
                        <td class="${stateCls}">${s.wr_state || '—'}</td>
                        <td class="${stateCls}">${s.bought_today ? '✅ Bought today' : (s.wr_state === 'OVERSOLD' ? '🟢 BUY signal' : '😐 No signal')}</td>
                        <td>${h.qty != null && h.qty > 0 ? h.qty : '—'}</td>
                        <td>${h.avg_price != null ? '₹' + Number(h.avg_price).toFixed(2) : '—'}</td>
                        <td>${h.current_value != null ? '₹' + Number(h.current_value).toLocaleString('en-IN', {maximumFractionDigits:0}) : '—'}</td>
                        <td class="${pnlCls}">${h.pnl_amt != null ? (h.pnl_amt >= 0 ? '+' : '') + '₹' + Number(h.pnl_amt).toFixed(2) : '—'}</td>
                        <td class="${pnlCls}">${h.pnl_pct != null ? (h.pnl_pct >= 0 ? '+' : '') + Number(h.pnl_pct).toFixed(2) + '%' : '—'}</td>
                        <td style="font-size:0.72rem;">${s.next_buy || '—'}</td>
                        <td>${s.total_deployed != null ? '₹' + Number(s.total_deployed).toLocaleString('en-IN', {maximumFractionDigits:0}) : '—'}</td>
                    </tr>`;
                }).join('');
            } else if (tbody) {
                tbody.innerHTML = '<tr><td colspan="12" class="empty-state">No symbols configured</td></tr>';
            }

            // ── Trade log ─────────────────────────────────────────────────────
            const trades = d.trade_log || [];
            if (trades.length) {
                const tBody = document.getElementById('bnh-trade-body');
                if (tBody) {
                    tBody.innerHTML = [...trades].reverse().map(t => {
                        const isBuy     = t.action === 'BUY';
                        const isPartial = t.action === 'PARTIAL_SELL';
                        return `<tr>
                            <td>${t.date || '—'}</td>
                            <td>${t.time || '—'}</td>
                            <td>${t.symbol || '—'}</td>
                            <td><span class="${isBuy ? 'tx-buy' : isPartial ? 'tx-partial' : 'tx-sell'} tx-type">${t.action || '—'}</span></td>
                            <td>₹${t.price != null ? t.price.toFixed(2) : '—'}</td>
                            <td>${t.qty != null ? t.qty : '—'}</td>
                            <td>₹${t.amount != null ? t.amount.toLocaleString('en-IN', {maximumFractionDigits:0}) : '—'}</td>
                            <td>${t.wr != null ? t.wr.toFixed(1) : '—'}</td>
                            <td>${t.funded_by || '—'}</td>
                            <td style="font-size:0.65rem;color:var(--text-secondary)">${t.reason || '—'}</td>
                        </tr>`;
                    }).join('');
                }
            }

            // ── Seed inputs ───────────────────────────────────────────────────
            const etfInp = document.getElementById('bnh-max-cash-etf');
            const txnInp = document.getElementById('bnh-max-cash-txn');
            if (etfInp && !etfInp.value && d.max_cash_per_etf) etfInp.value = d.max_cash_per_etf;
            if (txnInp && !txnInp.value && d.max_cash_per_txn) txnInp.value = d.max_cash_per_txn;

            const pctInp = document.getElementById('bnh-partial-profit-pct');
            const livePct = d.partial_profit_pct ?? 5;
            bnhHarvestPct = livePct;
            if (pctInp && (!pctInp.value || document.activeElement !== pctInp)) {
                if (!pctInp.value) pctInp.value = livePct;
            }
            ['bnh-subtitle-pct', 'bnh-ref-pct', 'bnh-kv-pct'].forEach(id => {
                const el = document.getElementById(id);
                if (el) el.textContent = livePct;
            });
        })
        .catch(() => {});
}

function startBnHRefresh() {
    if (intradayRefreshInterval) clearInterval(intradayRefreshInterval);
    intradayRefreshInterval = setInterval(loadBnHStatus, 5000);
    loadBnHStatus();
}

// ── Force Buy Now / Sell Now ─────────────────────────────────────────────────
function forceBuyNow() {
    const btn = document.getElementById('btn-buy-now');
    const res = document.getElementById('force-trade-result');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Executing buys...'; }
    if (res) { res.style.display = 'none'; }

    fetch('/api/force_buy_now', { method: 'POST' })
        .then(r => r.json())
        .then(d => {
            let msg = '';
            if (d.success) {
                const active = (d.results?.active_strategy || []);
                const bnh    = d.results?.bnh;
                const lines  = [];
                active.forEach(s => lines.push((s.success ? '✅' : '❌') + ' ' + s.symbol));
                if (bnh) lines.push(bnh.success
                    ? '✅ BnH symbols accumulated'
                    : '⚠️ BnH: ' + (bnh.reason || 'skipped'));
                if (!lines.length) lines.push('No symbols met buy conditions right now');
                msg = lines.join(' · ');
                showToast('Buy Now: ' + msg, d.success ? 'success' : 'warning');
            } else {
                msg = '❌ ' + (d.error || 'Failed');
                showToast(msg, 'error');
            }
            if (res) { res.textContent = msg; res.style.display = 'block'; }
        })
        .catch(() => {
            showToast('❌ Network error', 'error');
            if (res) { res.textContent = '❌ Network error'; res.style.display = 'block'; }
        })
        .finally(() => {
            if (btn) { btn.disabled = false; btn.textContent = '🟢 BUY NOW — Active Strategy + Dip Accumulator & Harvester'; }
        });
}

function forceSellNow() {
    const btn = document.getElementById('btn-sell-now');
    const res = document.getElementById('force-trade-result');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Executing sells...'; }
    if (res) { res.style.display = 'none'; }

    fetch('/api/force_sell_now', { method: 'POST' })
        .then(r => r.json())
        .then(d => {
            let msg = '';
            if (d.success) {
                const active = (d.results?.active_strategy || []);
                const lines  = active.map(s => (s.success ? '✅' : '❌') + ' ' + s.symbol);
                if (!lines.length) lines.push('No symbols met sell conditions right now');
                msg = lines.join(' · ');
                showToast('Sell Now: ' + msg, d.success ? 'success' : 'warning');
            } else {
                msg = '❌ ' + (d.error || 'Failed');
                showToast(msg, 'error');
            }
            if (res) { res.textContent = msg; res.style.display = 'block'; }
        })
        .catch(() => {
            showToast('❌ Network error', 'error');
            if (res) { res.textContent = '❌ Network error'; res.style.display = 'block'; }
        })
        .finally(() => {
            if (btn) { btn.disabled = false; btn.textContent = '🔴 SELL NOW — Active Strategy'; }
        });
}

// Aliases so the old wiring (tab switch, DOMContentLoaded) still works
function loadIntradayStatus() { loadBnHStatus(); }
function updateIntradayUI(running) { updateBnHUI(running); }
function startIntradayRefresh() { startBnHRefresh(); }

// Restore Dip Accumulator running state on page load
document.addEventListener('DOMContentLoaded', () => {
    fetch('/api/intraday/status').then(r => r.json()).then(d => {
        if (d.available) {
            updateBnHUI(d.running);
            if (d.running) startBnHRefresh();
        }
    }).catch(() => {});
});

// Hook into tab switches — start/stop polling when intraday tab opens/closes
(function() {
    const _orig = window.switchTab;
    if (typeof _orig === 'function') {
        window.switchTab = function(tab) {
            _orig(tab);
            if (tab === 'intraday') {
                startBnHRefresh();
            } else {
                if (intradayRefreshInterval) {
                    clearInterval(intradayRefreshInterval);
                    intradayRefreshInterval = null;
                }
            }
        };
    }
})();

// Function to handle daily Enctoken reset
// ── Pause / Resume Bot ────────────────────────────────────────────────────────
// Pause stops ALL frontend polling intervals and tells the backend to skip
// Zerodha OMS calls. This lets you log into Kite without the bot's API calls
// competing with / invalidating your browser session.
// Resume restarts everything exactly as it was.

let _botPaused = false;

async function togglePauseResume() {
    if (_botPaused) {
        await _resumeBot();
    } else {
        await _pauseBot();
    }
}

async function _pauseBot() {
    try {
        const r = await fetch('/api/bot/pause', { method: 'POST',
            headers: { 'Content-Type': 'application/json' } });
        if (!r.ok) { showToast('Failed to pause bot.', 'error'); return; }
    } catch(e) { showToast('Error pausing bot.', 'error'); return; }

    // Stop all frontend polling intervals
    if (typeof refreshInterval     !== 'undefined') clearInterval(refreshInterval);
    if (typeof transactionInterval !== 'undefined') clearInterval(transactionInterval);
    if (typeof syncInterval        !== 'undefined') clearInterval(syncInterval);
    if (typeof intradayRefreshInterval !== 'undefined') clearInterval(intradayRefreshInterval);
    if (typeof autoBuyInterval     !== 'undefined') clearInterval(autoBuyInterval);
    refreshInterval = transactionInterval = syncInterval = intradayRefreshInterval = autoBuyInterval = null;

    _botPaused = true;

    // Update button
    const btn = document.getElementById('pause-resume-btn');
    if (btn) {
        btn.textContent = '▶ Resume Bot';
        btn.style.borderColor = '#22c55e';
        btn.style.color = '#22c55e';
        btn.title = 'Resume all bot activity';
    }

    // Update status badge
    const badge = document.getElementById('bot-status-badge');
    if (badge) {
        badge.textContent = '⏸ PAUSED';
        badge.style.background = '#6b7280';
        badge.style.opacity = '1';
        badge.style.display = 'inline-flex';
    }

    showToast('Bot paused — all Zerodha API calls stopped. Log into Kite freely, then click Resume Bot.', 'info', 6000);
}

async function _resumeBot() {
    try {
        const r = await fetch('/api/bot/resume', { method: 'POST',
            headers: { 'Content-Type': 'application/json' } });
        if (!r.ok) { showToast('Failed to resume bot.', 'error'); return; }
    } catch(e) { showToast('Error resuming bot.', 'error'); return; }

    _botPaused = false;

    // Restart all polling
    if (typeof startAutoRefresh === 'function') startAutoRefresh();

    // Update button
    const btn = document.getElementById('pause-resume-btn');
    if (btn) {
        btn.textContent = '⏸ Pause Bot';
        btn.style.borderColor = '#f97316';
        btn.style.color = '#f97316';
        btn.title = 'Pause all Zerodha API calls so you can log into Kite without being logged out';
    }

    // Immediately refresh everything
    if (typeof loadStatus    === 'function') loadStatus();
    if (typeof loadPortfolio === 'function') loadPortfolio();
    if (typeof loadPositions === 'function') loadPositions();

    showToast('Bot resumed — all systems active.', 'success');
}

async function logoutSession() {
    if (!confirm("Are you sure you want to reset the session? This will stop the bot and require you to enter a fresh Enctoken.")) {
        return;
    }
    
    try {
        const response = await fetch('/api/auth/logout', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            }
        });
        
        if (response.ok) {
            // Success! The token is deleted from the server. Reload to go to the login page.
            window.location.href = "/";
        } else {
            showToast("Failed to logout.", "error");
        }
    } catch (error) {
        console.error("Logout error:", error);
        showToast("Error connecting to server.", "error");
    }
}

function saveCashReserve() {
    const input = document.getElementById('bot-cash-reserve');
    if (!input) return;
    const val = parseFloat(input.value);
    if (isNaN(val) || val < 0) {
        showToast('Cash Reserve must be ₹0 or more', 'error');
        return;
    }
    fetch('/api/settings/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cash_reserve: val })
    })
    .then(r => r.json())
    .then(d => {
        if (d.status === 'success') {
            showToast('✅ Cash Reserve set to ₹' + val.toLocaleString('en-IN'), 'success');
        } else {
            showToast(d.error || 'Failed to save Cash Reserve', 'error');
        }
    })
    .catch(() => showToast('Failed to save Cash Reserve', 'error'));
}

// ════════════════════════════════════════════════════════════════
//  MANAGE SYMBOLS TAB
// ════════════════════════════════════════════════════════════════

// ── NSE Symbol master lists (ETFs + NIFTY 200 + NIFTY MIDCAP 150) ──────────────────────────
const NSE_ETF_LIST = ["ABSL10BANK", "ABSLBANETF", "ABSLMSCIN", "ABSLNN50ET", "ABSLPSE", "ALPHA", "ALPHAETF", "ALPL30IETF", "AONEGOLD", "AONENIFTY", "AONESILVER", "AONETMMQ50", "AONETOTAL", "AUTOBEES", "AUTOIETF", "AXISBNKETF", "AXISCETF", "AXISGOLD", "AXISHCETF", "AXISILVER", "AXISNIFTY", "AXISTECETF", "AXISVALUE", "AXSENSEX", "BANK10ADD", "BANKADD", "BANKBEES", "BANKBETA", "BANKBETF", "BANKETF", "BANKIETF", "BANKNIFTY1", "BANKPSU", "BBNPNBETF", "BBNPPGOLD", "BFSI", "BSE500IETF", "BSLGOLDETF", "BSLNIFTY", "BSLSENETFG", "CHEMICAL", "CHOICEGOLD", "COMMOIETF", "CONS", "CONSUMBEES", "CONSUMER", "CONSUMIETF", "CPSEETF", "DEFENCE", "DIVIDEND", "DIVOPPBEES", "EBANKNIFTY", "ECAPINSURE", "EGOLD", "ELM250", "EMULTIMQ", "ENERGY", "ENIFTY", "EQUAL200", "EQUAL50", "EQUAL50ADD", "ESENSEX", "ESG", "ESILVER", "EVIETF", "EVINDIA", "FINIETF", "FLEXIADD", "FMCGADD", "FMCGIETF", "GOLD1", "GOLD360", "GOLDADD", "GOLDBEES", "GOLDBETA", "GOLDBND", "GOLDCASE", "GOLDETF", "GOLDIETF", "GROWWCAPM", "GROWWCHEM", "GROWWDEFNC", "GROWWEV", "GROWWGOLD", "GROWWHOSPI", "GROWWLOVOL", "GROWWMC150", "GROWWMETAL", "GROWWMOM50", "GROWWN200", "GROWWNET", "GROWWNIFTY", "GROWWNXT50", "GROWWPOWER", "GROWWPSE", "GROWWPSUBK", "GROWWRAIL", "GROWWRLTY", "GROWWSC250", "GROWWSLVR", "HDFCBSE500", "HDFCGOLD", "HDFCGROWTH", "HDFCLOWVOL", "HDFCMID150", "HDFCMOMENT", "HDFCNEXT50", "HDFCNIF100", "HDFCNIFBAN", "HDFCNIFIT", "HDFCNIFTY", "HDFCPSUBK", "HDFCPVTBAN", "HDFCQUAL", "HDFCSENSEX", "HDFCSILVER", "HDFCSML250", "HDFCVALUE", "HEALTHADD", "HEALTHCARE", "HEALTHIETF", "HEALTHY", "HNGSNGBEES", "HSBCGOLD", "ICICIB22", "IDFNIFTYET", "INFRA", "INFRABEES", "INFRAIETF", "INTERNET", "IT", "ITADD", "ITBEES", "ITBETA", "ITETF", "ITIETF", "IVZINGOLD", "IVZINNIFTY", "JUNIORBEES", "LICMFGOLD", "LICNETFGSC", "LICNETFN50", "LICNETFSEN", "LICNFNHGP", "LICNMID100", "LOWVOL", "LOWVOL1", "LOWVOLIETF", "MAFANG", "MAHKTECH", "MAKEINDIA", "MANUFGBEES", "MASPTOP50", "METAL", "METALIETF", "MID150", "MID150BEES", "MID150CASE", "MIDCAP", "MIDCAPADD", "MIDCAPBETA", "MIDCAPETF", "MIDCAPIETF", "MIDQ50ADD", "MIDSELIETF", "MIDSMALL", "MNC", "MOALPHA50", "MOBANK10", "MOCAPITAL", "MODEFENCE", "MOENERGY", "MOGOLD", "MOHEALTH", "MOINFRA", "MOIPO", "MOLOWVOL", "MOM100", "MOM30IETF", "MOM50", "MOMENTUM", "MOMENTUM30", "MOMENTUM50", "MOMGF", "MOMIDMTM", "MOMNC", "MOMOMENTUM", "MON100", "MON50EQUAL", "MONEXT50", "MONIFTY100", "MONIFTY500", "MONQ50", "MOPSE", "MOQUALITY", "MOREALTY", "MOSERVICE", "MOSILVER", "MOSMALL250", "MOTOUR", "MOVALUE", "MSCIADD", "MSCIINDIA", "MULTICAP", "NETF", "NEXT30ADD", "NEXT50", "NEXT50ADD", "NEXT50BETA", "NEXT50ETF", "NEXT50IETF", "NIF100BEES", "NIF100IETF", "NIFTY1", "NIFTY100EW", "NIFTYADD", "NIFTYBEES", "NIFTYBETA", "NIFTYBETF", "NIFTYCASE", "NIFTYETF", "NIFTYIETF", "NIFTYQLITY", "NV20", "NV20BEES", "NV20IETF", "OILIETF", "PHARMABEES", "PSUBANK", "PSUBANKADD", "PSUBNKBEES", "PSUBNKIETF", "PVTBANIETF", "PVTBANKADD", "QGOLDHALF", "QNIFTY", "QUAL30IETF", "QUALITY30", "SBIBPB", "SBIETFCON", "SBIETFIT", "SBIETFPB", "SBIETFQLTY", "SBIMIDMOM", "SBINEQWETF", "SBINMID150", "SBISILVER", "SELECTIPO", "SENSEXADD", "SENSEXBETA", "SENSEXETF", "SENSEXIETF", "SETFGOLD", "SETFNIF50", "SETFNIFBK", "SETFNN50", "SHARIABEES", "SILVER", "SILVER1", "SILVER360", "SILVERADD", "SILVERAG", "SILVERBEES", "SILVERBETA", "SILVERBND", "SILVERCASE", "SILVERIETF", "SMALL250", "SMALLADD", "SMALLCAP", "SML100CASE", "SNXT30BEES", "SNXT50BETA", "TATAGOLD", "TATSILV", "TECH", "TNIDETF", "TOP100CASE", "TOP10ADD", "TOP15IETF", "TOP20", "TWCGOLDETF", "UNIONGOLD", "VAL30IETF", "VALUE"];
const NSE_NIFTY200_LIST = ["360ONE", "ABB", "ABCAPITAL", "ADANIENSOL", "ADANIENT", "ADANIGREEN", "ADANIPORTS", "ADANIPOWER", "ALKEM", "AMBUJACEM", "APLAPOLLO", "APOLLOHOSP", "ASHOKLEY", "ASIANPAINT", "ASTRAL", "ATGL", "AUBANK", "AUROPHARMA", "AXISBANK", "BAJAJ-AUTO", "BAJAJFINSV", "BAJAJHLDNG", "BAJFINANCE", "BANKBARODA", "BANKINDIA", "BDL", "BEL", "BHARATFORG", "BHARTIARTL", "BHEL", "BIOCON", "BLUESTARCO", "BOSCHLTD", "BPCL", "BRITANNIA", "BSE", "CANBK", "CGPOWER", "CHOLAFIN", "CIPLA", "COALINDIA", "COCHINSHIP", "COFORGE", "COLPAL", "CONCOR", "COROMANDEL", "CUMMINSIND", "DABUR", "DIVISLAB", "DIXON", "DLF", "DMART", "DRREDDY", "EICHERMOT", "ENRIN", "ETERNAL", "EXIDEIND", "FEDERALBNK", "FORTIS", "GAIL", "GLENMARK", "GMRAIRPORT", "GODFRYPHLP", "GODREJCP", "GODREJPROP", "GRASIM", "GROWW", "GVT&D", "HAL", "HAVELLS", "HCLTECH", "HDFCAMC", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO", "HINDALCO", "HINDPETRO", "HINDUNILVR", "HINDZINC", "HUDCO", "HYUNDAI", "ICICIAMC", "ICICIBANK", "ICICIGI", "IDEA", "IDFCFIRSTB", "INDHOTEL", "INDIANB", "INDIGO", "INDUSINDBK", "INDUSTOWER", "INFY", "IOC", "IRCTC", "IREDA", "IRFC", "ITC", "JINDALSTEL", "JIOFIN", "JSWENERGY", "JSWSTEEL", "JUBLFOOD", "KALYANKJIL", "KEI", "KOTAKBANK", "KPITTECH", "LAURUSLABS", "LENSKART", "LGEINDIA", "LICHSGFIN", "LODHA", "LT", "LTF", "LTM", "LUPIN", "M&M", "M&MFIN", "MANKIND", "MARICO", "MARUTI", "MAXHEALTH", "MAZDOCK", "MCX", "MFSL", "MOTHERSON", "MOTILALOFS", "MPHASIS", "MRF", "MUTHOOTFIN", "NATIONALUM", "NAUKRI", "NESTLEIND", "NHPC", "NMDC", "NTPC", "NYKAA", "OBEROIRLTY", "OFSS", "OIL", "ONGC", "PAGEIND", "PATANJALI", "PAYTM", "PERSISTENT", "PFC", "PHOENIXLTD", "PIDILITIND", "PIIND", "PNB", "POLICYBZR", "POLYCAB", "POWERGRID", "POWERINDIA", "PREMIERENE", "PRESTIGE", "RADICO", "RECLTD", "RELIANCE", "RVNL", "SAIL", "SBICARD", "SBILIFE", "SBIN", "SHREECEM", "SHRIRAMFIN", "SIEMENS", "SOLARINDS", "SRF", "SUNPHARMA", "SUPREMEIND", "SUZLON", "SWIGGY", "TATACAP", "TATACOMM", "TATACONSUM", "TATAELXSI", "TATAINVEST", "TATAPOWER", "TATASTEEL", "TCS", "TECHM", "TIINDIA", "TITAN", "TMCV", "TMPV", "TORNTPHARM", "TRENT", "TVSMOTOR", "ULTRACEMCO", "UNIONBANK", "UNITDSPR", "UPL", "VBL", "VEDL", "VMM", "VOLTAS", "WAAREEENER", "WIPRO", "YESBANK", "ZYDUSLIFE"];
const NSE_MIDCAP150_LIST = ["360ONE", "3MINDIA", "ACC", "AIAENG", "APLAPOLLO", "AUBANK", "AWL", "ABBOTINDIA", "ATGL", "ABCAPITAL", "AJANTPHARM", "ALKEM", "ANTHEM", "APARINDS", "APOLLOTYRE", "ASHOKLEY", "ASTRAL", "AUROPHARMA", "AIIL", "BSE", "BAJAJHFL", "BALKRISIND", "BANKINDIA", "MAHABANK", "BERGEPAINT", "BDL", "BHARATFORG", "BHEL", "BHARTIHEXA", "GROWW", "BIOCON", "BLUESTARCO", "CRISIL", "COCHINSHIP", "COFORGE", "COLPAL", "CONCOR", "COROMANDEL", "DABUR", "DALBHARAT", "DIXON", "ENDURANCE", "ESCORTS", "EXIDEIND", "NYKAA", "FEDERALBNK", "FORTIS", "GVT&D", "GMRAIRPORT", "GICRE", "GLAXO", "GLENMARK", "MEDANTA", "GODFRYPHLP", "GODREJIND", "GODREJPROP", "FLUOROCHEM", "HDBFS", "HAVELLS", "HEROMOTOCO", "HEXT", "HINDPETRO", "POWERINDIA", "HONAUT", "HUDCO", "ICICIGI", "ICICIAMC", "ICICIPRULI", "IDFCFIRSTB", "ITCHOTELS", "INDIANB", "IRCTC", "IREDA", "INDUSTOWER", "INDUSINDBK", "NAUKRI", "IPCALAB", "JKCEMENT", "JSWENERGY", "JSWINFRA", "JSL", "JUBLFOOD", "KPRMILL", "KEI", "KPITTECH", "KALYANKJIL", "LTF", "LTTS", "LGEINDIA", "LICHSGFIN", "LAURUSLABS", "LENSKART", "LICI", "LINDEINDIA", "LLOYDSME", "LUPIN", "MRF", "M&MFIN", "MANKIND", "MARICO", "MFSL", "MOTILALOFS", "MPHASIS", "MCX", "NHPC", "NLCINDIA", "NMDC", "NTPCGREEN", "NATIONALUM", "NAM-INDIA", "OBEROIRLTY", "OIL", "PAYTM", "OFSS", "POLICYBZR", "PIIND", "PAGEIND", "PATANJALI", "PERSISTENT", "PETRONET", "PHOENIXLTD", "POLYCAB", "PREMIERENE", "PRESTIGE", "RADICO", "RVNL", "SBICARD", "SJVN", "SRF", "SCHAEFFLER", "SAIL", "SUNDARMFIN", "SUPREMEIND", "SUZLON", "SWIGGY", "TATACOMM", "TATAELXSI", "TATAINVEST", "NIACL", "THERMAX", "TORNTPOWER", "TIINDIA", "UNOMINDA", "UPL", "UBL", "VMM", "IDEA", "VOLTAS", "WAAREEENER", "YESBANK"];
const NSE_STK_LIST = [...new Set([...NSE_NIFTY200_LIST, ...NSE_MIDCAP150_LIST])].sort();
const NSE_ALL_SYMBOLS = [...new Set([...NSE_ETF_LIST, ...NSE_STK_LIST])].sort();


const _symState = {
    active: [],   // current list being edited
    bnh:    [],
};

// Load symbols from server and render both lists
function loadSymbols() {
    fetch('/api/symbols/get')
        .then(r => r.json())
        .then(d => {
            if (!d.success) return;
            _symState.active = [...(d.active_etfs || [])];
            _symState.bnh    = [...(d.bnh_symbols  || [])];
            _renderSymList('active');
            _renderSymList('bnh');
        })
        .catch(() => {});
}

// Render symbols as a card grid + mirror into Controls tab Universe row
function _renderSymList(strategy) {
    const list = document.getElementById(`${strategy}-sym-list`);
    const meta = document.getElementById(`${strategy}-sym-meta`);
    const syms = _symState[strategy];
    if (!list) return;

    if (!syms.length) {
        list.innerHTML = '<p class="sym-empty-msg">No symbols added yet</p>';
    } else {
        list.innerHTML = syms.map((sym, idx) => `
            <div class="sym-card">
                <span class="sym-card-name">${sym}</span>
                <button class="sym-card-del" title="Remove ${sym}"
                        onclick="removeSymbol('${strategy}', ${idx})">✕</button>
            </div>`).join('');
    }
    if (meta) meta.textContent = `${syms.length} symbol${syms.length !== 1 ? 's' : ''}`;
    _updateUniverseRow(strategy, syms);
}

// Update the Universe row in the Controls tab strategy write-up
function _updateUniverseRow(strategy, syms) {
    const elId = strategy === 'active' ? 'ctrl-active-universe' : 'ctrl-bnh-universe';
    const el   = document.getElementById(elId);
    if (!el) return;
    if (!syms || !syms.length) { el.textContent = '—'; return; }
    const chips = syms.map(s => `<span class="ctrl-universe-chip">${s}</span>`).join(' ');
    if (strategy === 'active') {
        el.innerHTML = `<span class="ctrl-universe-chips">${chips}</span>`;
    } else {
        el.innerHTML = `<span class="ctrl-universe-chips">${chips}</span>`
            + `<span class="ctrl-universe-note"> · NSE · CNC · each symbol tracked independently</span>`;
    }
}

// ── Searchable dropdown helper ────────────────────────────────────────────────

// ── Dropdown portal (appended to <body> so it's never clipped) ───────────────
const _ddPortals = {};

function _getPortal(strategy) {
    if (_ddPortals[strategy]) return _ddPortals[strategy];
    const dd = document.createElement('div');
    dd.id        = `portal-dd-${strategy}`;
    dd.className = 'sym-dropdown';
    dd.style.cssText = 'display:none; position:fixed; z-index:999999;';
    document.body.appendChild(dd);
    _ddPortals[strategy] = dd;
    return dd;
}

function _buildDropdown(strategy) {
    const inp = document.getElementById(`${strategy}-sym-input`);
    if (!inp) return;

    const dd    = _getPortal(strategy);
    const query = (inp.value || '').trim().toUpperCase();

    // Position under input
    const rect = inp.getBoundingClientRect();
    dd.style.top   = (rect.bottom + window.scrollY + 3) + 'px';
    dd.style.left  = rect.left + 'px';
    dd.style.width = Math.max(rect.width + 80, 320) + 'px';

    // Filter — show ALL matches (virtual scroll handles perf)
    const usedActive = new Set(_symState.active);
    const usedBnh    = new Set(_symState.bnh);

    // Build grouped matches with section headers for display
    let matchGroups; // [{header, syms}]
    if (query.length === 0) {
        matchGroups = [
            { header: '📊 ETFs',                  syms: [...NSE_ETF_LIST] },
            { header: '🏦 NIFTY 200 Stocks',       syms: [...NSE_NIFTY200_LIST] },
            { header: '📈 NIFTY MIDCAP 150 Stocks', syms: [...NSE_MIDCAP150_LIST] },
        ];
    } else {
        const sw200  = NSE_NIFTY200_LIST.filter(s =>  s.startsWith(query));
        const swMid  = NSE_MIDCAP150_LIST.filter(s => s.startsWith(query) && !sw200.includes(s));
        const swEtf  = NSE_ETF_LIST.filter(s =>       s.startsWith(query));
        const c200   = NSE_NIFTY200_LIST.filter(s =>  !s.startsWith(query) && s.includes(query));
        const cMid   = NSE_MIDCAP150_LIST.filter(s => !s.startsWith(query) && s.includes(query) && !c200.includes(s));
        const cEtf   = NSE_ETF_LIST.filter(s =>       !s.startsWith(query) && s.includes(query));
        matchGroups = [
            { header: '📊 ETFs',                  syms: [...swEtf,  ...cEtf]  },
            { header: '🏦 NIFTY 200 Stocks',       syms: [...sw200,  ...c200]  },
            { header: '📈 NIFTY MIDCAP 150 Stocks', syms: [...swMid,  ...cMid]  },
        ].filter(g => g.syms.length > 0);
    }
    // Flatten for virtual scroll, keeping header markers
    const matches = [];
    for (const g of matchGroups) {
        matches.push({ _header: g.header });
        g.syms.forEach(s => matches.push(s));
    }

    if (!matches.length) {
        dd.innerHTML = '<div class="sym-dd-empty">No matches found</div>';
        dd.style.display = 'block';
        _startPosLoop(strategy, inp);
        return;
    }

    // Virtual scroll: render visible slice, scroll to load more
    const CHUNK = 60;
    let rendered = Math.min(CHUNK, matches.length);

    function renderItems(count) {
        const items = matches.slice(0, count).map(item => {
            if (item && item._header) {
                return `<div class="sym-dd-group-header">${item._header}</div>`;
            }
            const sym = item;
            const inActive  = usedActive.has(sym);
            const inBnh     = usedBnh.has(sym);
            const inThis    = strategy === 'active' ? inActive : inBnh;
            const inOther   = strategy === 'active' ? inBnh    : inActive;
            const otherLbl  = strategy === 'active' ? 'Dip Acc.' : 'Active';
            const isEtf     = NSE_ETF_LIST.includes(sym);
            const badgeCls  = isEtf ? 'etf' : 'stk';
            const badgeTxt  = isEtf ? 'ETF' : 'Stock';

            let cls   = 'sym-dd-item';
            let extra = '';
            if (inThis)  { cls += ' sym-dd-used';  extra = '<span class="sym-dd-tag">Added</span>'; }
            if (inOther) { cls += ' sym-dd-other'; extra = `<span class="sym-dd-tag warn">In ${otherLbl}</span>`; }

            return `<div class="${cls}" onmousedown="event.preventDefault();_selectDropdown('${strategy}','${sym}')">` +
                   `<span class="sym-dd-badge ${badgeCls}">${badgeTxt}</span>` +
                   `<span class="sym-dd-name">${sym}</span>${extra}</div>`;
        }).join('');
        const footer = count < matches.length
            ? `<div class="sym-dd-footer">${matches.length - count} more — keep typing to filter</div>` : '';
        dd.innerHTML = items + footer;
    }

    renderItems(rendered);
    dd.style.display = 'block';

    // Load more on scroll
    dd.onscroll = () => {
        if (dd.scrollTop + dd.clientHeight >= dd.scrollHeight - 40 && rendered < matches.length) {
            rendered = Math.min(rendered + CHUNK, matches.length);
            renderItems(rendered);
        }
    };

    _startPosLoop(strategy, inp);
}

// Keep portal positioned correctly as page scrolls
const _posLoops = {};
function _startPosLoop(strategy, inp) {
    if (_posLoops[strategy]) return;
    _posLoops[strategy] = setInterval(() => {
        const dd = _ddPortals[strategy];
        if (!dd || dd.style.display === 'none') {
            clearInterval(_posLoops[strategy]);
            delete _posLoops[strategy];
            return;
        }
        const rect = inp.getBoundingClientRect();
        dd.style.top  = (rect.bottom + window.scrollY + 3) + 'px';
        dd.style.left = rect.left + 'px';
    }, 80);
}

function _selectDropdown(strategy, sym) {
    _hideDropdown(strategy);
    // Check same-strategy duplicate
    if (_symState[strategy].includes(sym)) {
        showToast(`${sym} is already in this list`, 'warning');
        return;
    }
    // Check cross-strategy duplicate
    const other = strategy === 'active' ? _symState.bnh : _symState.active;
    const otherLabel = strategy === 'active' ? 'Dip Accumulator & Harvester' : 'Active Strategy';
    if (other.includes(sym)) {
        showToast(`⚠️ ${sym} is already in ${otherLabel}`, 'error');
        return;
    }
    _symState[strategy].push(sym);
    _renderSymList(strategy);
    _markUnsaved(strategy);
    const inp = document.getElementById(`${strategy}-sym-input`);
    if (inp) { inp.value = ''; inp.focus(); }
}

function _hideDropdown(strategy) {
    const dd = _ddPortals[strategy];
    if (dd) dd.style.display = 'none';
    if (_posLoops[strategy]) {
        clearInterval(_posLoops[strategy]);
        delete _posLoops[strategy];
    }
    // Also hide the original in-DOM placeholder (kept for HTML structure)
    const orig = document.getElementById(`${strategy}-sym-dropdown`);
    if (orig) orig.style.display = 'none';
}

// ── Add symbol ────────────────────────────────────────────────────────────────

function addSymbol(strategy) {
    const inp = document.getElementById(`${strategy}-sym-input`);
    if (!inp) return;
    const val = inp.value.trim().toUpperCase().replace(/[^A-Z0-9\-&]/g, '');
    if (!val) { showToast('Enter or select a symbol first', 'warning'); return; }
    _selectDropdown(strategy, val);
    _hideDropdown(strategy);
}

// ── Filtered dropdown for the 3-group symbol pickers ──────────────────────────
// Uses `.sym-fdd` divs that live inside `.sym-dropdown-group` (position:relative)
// so they naturally sit below the input row and never overlap the Add button.

function _buildFDD(strategy, listKey, inputEl) {
    const ddId = `${strategy}-fdd-${listKey}`;
    const dd   = document.getElementById(ddId);
    if (!dd) return;

    const query = (inputEl.value || '').trim().toUpperCase();
    const pool  = listKey === 'etf' ? NSE_ETF_LIST
                : listKey === 'n200' ? NSE_NIFTY200_LIST
                : NSE_MIDCAP150_LIST;

    const usedThis  = new Set(_symState[strategy]);
    const usedOther = new Set(strategy === 'active' ? _symState.bnh : _symState.active);
    const otherLbl  = strategy === 'active' ? 'Dip Acc.' : 'Active';

    let filtered;
    if (!query) {
        filtered = [...pool];
    } else {
        filtered = [
            ...pool.filter(s => s.startsWith(query)),
            ...pool.filter(s => !s.startsWith(query) && s.includes(query))
        ];
    }

    if (!filtered.length) {
        dd.innerHTML = '<div class="sym-fdd-empty">No matches</div>';
        dd.style.display = 'block';
        return;
    }

    const CHUNK = 50;
    let rendered = Math.min(CHUNK, filtered.length);

    function renderItems(n) {
        const rows = filtered.slice(0, n).map(sym => {
            const inThis  = usedThis.has(sym);
            const inOther = usedOther.has(sym);
            let cls   = 'sym-fdd-item';
            let extra = '';
            if (inThis)       { cls += ' already-in'; extra = ' <span class="sym-dd-in-use">✓</span>'; }
            else if (inOther) { cls += ' in-other';   extra = ` <span class="sym-dd-in-use">${otherLbl}</span>`; }
            const handler = inThis ? '' : `onmousedown="event.preventDefault();_selectFDD('${strategy}','${sym}','${listKey}')"`;
            return `<div class="${cls}" ${handler}>${sym}${extra}</div>`;
        }).join('');
        const footer = n < filtered.length
            ? `<div class="sym-fdd-footer">${filtered.length - n} more — keep typing to narrow</div>` : '';
        dd.innerHTML = rows + footer;
    }

    renderItems(rendered);
    dd.style.display = 'block';

    dd.onscroll = () => {
        if (dd.scrollTop + dd.clientHeight >= dd.scrollHeight - 30 && rendered < filtered.length) {
            rendered = Math.min(rendered + CHUNK, filtered.length);
            renderItems(rendered);
        }
    };
}

function _closeFDD(strategy, listKey) {
    const dd = document.getElementById(`${strategy}-fdd-${listKey}`);
    if (dd) dd.style.display = 'none';
}

function _closeAllFDDs() {
    document.querySelectorAll('.sym-fdd').forEach(el => el.style.display = 'none');
}

// Close FDDs when clicking outside
document.addEventListener('mousedown', (e) => {
    if (!e.target.closest('.sym-dropdown-group')) _closeAllFDDs();
});

function _selectFDD(strategy, sym, listKey) {
    _closeFDD(strategy, listKey);
    if (_symState[strategy].includes(sym)) {
        showToast(`${sym} is already in this list`, 'warning');
        return;
    }
    const other      = strategy === 'active' ? _symState.bnh : _symState.active;
    const otherLabel = strategy === 'active' ? 'Dip Accumulator & Harvester' : 'Active Strategy';
    if (other.includes(sym)) {
        showToast(`⚠️ ${sym} is already in ${otherLabel}`, 'error');
        return;
    }
    _symState[strategy].push(sym);
    _renderSymList(strategy);
    _markUnsaved(strategy);
    const inp = document.getElementById(`${strategy}-sym-input-${listKey}`);
    if (inp) { inp.value = ''; inp.focus(); }
}

function _addFromInput(strategy, listKey) {
    const inp = document.getElementById(`${strategy}-sym-input-${listKey}`);
    if (!inp) return;
    const val = inp.value.trim().toUpperCase().replace(/[^A-Z0-9\-&]/g, '');
    if (!val) { showToast('Enter or select a symbol first', 'warning'); return; }
    _closeFDD(strategy, listKey);
    _selectFDD(strategy, val, listKey);
}

// Keep old _buildDropdownFiltered as alias (unused but prevents errors if called elsewhere)
function _buildDropdownFiltered(strategy, listKey, inputEl) { _buildFDD(strategy, listKey, inputEl); }
function _selectDropdownFiltered(strategy, sym, listKey)    { _selectFDD(strategy, sym, listKey); }


function removeSymbol(strategy, idx) {
    const sym = _symState[strategy][idx];
    _symState[strategy].splice(idx, 1);
    _renderSymList(strategy);
    _markUnsaved(strategy);
    showToast(`Removed ${sym}`, 'info');
}

function _markUnsaved(strategy) {
    const status = document.getElementById(`${strategy}-save-status`);
    if (status) {
        status.textContent = '● Unsaved changes';
        status.className   = 'sym-save-status error';
    }
}

// ── Save symbols ──────────────────────────────────────────────────────────────

function saveSymbols(strategy) {
    const btn    = document.getElementById(`${strategy}-save-btn`);
    const status = document.getElementById(`${strategy}-save-status`);
    const syms   = _symState[strategy];

    if (!syms.length) { showToast('Cannot save an empty symbol list', 'error'); return; }

    if (btn) { btn.disabled = true; btn.textContent = '⏳ Saving...'; }

    fetch('/api/symbols/update', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ strategy, symbols: syms }),
    })
    .then(r => r.json())
    .then(d => {
        if (d.success) {
            if (status) { status.textContent = '✅ Saved'; status.className = 'sym-save-status'; }
            _updateUniverseRow(strategy, syms);
            const stratLabel = strategy === 'active' ? 'Active Strategy' : 'Dip Accumulator';
            let msg = `✅ ${stratLabel} symbols saved — ${syms.length} symbols`;
            if (d.not_found && d.not_found.length) {
                msg += ` (⚠️ token lookup pending for: ${d.not_found.join(', ')})`;
            }
            showToast(msg, 'success');
            setTimeout(() => { if (status) status.textContent = ''; }, 4000);

            // ── Auto-refresh live data immediately ───────────────────────
            if (strategy === 'active') {
                // Clear row cache so deleted symbols are removed and new ones
                // get a fresh row — without this, incremental update keeps
                // stale rows and misses new ones.
                rowCache.clear();
                loadMarketData();
                // Second refresh after 8s to catch async historical/realtime load
                setTimeout(() => { rowCache.clear(); loadMarketData(); }, 8000);
                // Third refresh after 20s for symbols that needed token fetch
                setTimeout(() => { loadMarketData(); }, 20000);
            } else {
                bnhLatestCache = null;
                loadBnHStatus();
                setTimeout(() => { loadBnHStatus(); }, 8000);
                setTimeout(() => { loadBnHStatus(); }, 20000);
            }
        } else {
            if (status) { status.textContent = '❌ Save failed'; status.className = 'sym-save-status error'; }
            showToast('❌ ' + (d.error || 'Save failed'), 'error');
        }
    })
    .catch(() => {
        if (status) { status.textContent = '❌ Network error'; status.className = 'sym-save-status error'; }
        showToast('❌ Network error saving symbols', 'error');
    })
    .finally(() => {
        if (btn) { btn.disabled = false; btn.textContent = `💾 Save ${strategy === 'active' ? 'Active Strategy' : 'Dip Accumulator'} Symbols`; }
    });
}

// Load symbols on page load so Universe rows in Controls tab are populated immediately,
// and refresh again each time the Manage Symbols tab is clicked.
document.addEventListener('DOMContentLoaded', () => {
    loadSymbols();
    document.querySelectorAll('.tab-btn[data-tab="symbols"]').forEach(btn => {
        btn.addEventListener('click', loadSymbols);
    });

    // Close dropdowns on outside click
    document.addEventListener('click', e => {
        ['active', 'bnh'].forEach(strategy => {
            const wrap   = document.getElementById(`${strategy}-sym-input-wrap`);
            const portal = document.getElementById(`portal-dd-${strategy}`);
            if (wrap && !wrap.contains(e.target) && portal && !portal.contains(e.target)) {
                _hideDropdown(strategy);
            }
        });
    });
});

// ════════════════════════════════════════════════════════════════
//  W%R SCANNER
// ════════════════════════════════════════════════════════════════

let _scanResults = [];      // raw results from last scan

function _getScanSymbols() {
    const useEtf  = document.getElementById('scn-chk-etf')?.checked;
    const use200  = document.getElementById('scn-chk-n200')?.checked;
    const useMid  = document.getElementById('scn-chk-mid150')?.checked;
    const syms = new Set();
    if (useEtf)  NSE_ETF_LIST.forEach(s => syms.add(s));
    if (use200)  NSE_NIFTY200_LIST.forEach(s => syms.add(s));
    if (useMid)  NSE_MIDCAP150_LIST.forEach(s => syms.add(s));
    return [...syms];
}

function runScanner() {
    const symbols = _getScanSymbols();
    const dailyT  = parseFloat(document.getElementById('scn-daily-thresh')?.value  || '-60');
    const weeklyT = parseFloat(document.getElementById('scn-weekly-thresh')?.value || '-70');

    // UI: enter scanning state
    const runBtn = document.getElementById('scn-run-btn');
    if (runBtn) { runBtn.disabled = true; runBtn.textContent = '⏳ Scanning…'; }
    document.getElementById('scn-progress-wrap').style.display = 'flex';
    document.getElementById('scn-summary').style.display       = 'none';
    document.getElementById('scn-add-bar').style.display       = 'none';
    document.getElementById('scanner-results').style.display   = 'none';
    document.getElementById('scn-empty').style.display         = 'none';

    // Animate progress bar (indeterminate-style while waiting for response)
    let pct = 0;
    const fillEl  = document.getElementById('scn-progress-fill');
    const labelEl = document.getElementById('scn-progress-label');
    if (fillEl)  fillEl.style.width = '5%';
    if (labelEl) labelEl.textContent = `Scanning ${symbols.length} symbols…`;

    const progTimer = setInterval(() => {
        pct = Math.min(pct + 2, 90);
        if (fillEl) fillEl.style.width = pct + '%';
    }, 200);

    fetch('/api/scanner', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
            symbols,
            daily_threshold:  dailyT,
            weekly_threshold: weeklyT,
        }),
    })
    .then(r => r.json())
    .then(d => {
        clearInterval(progTimer);
        if (fillEl) fillEl.style.width = '100%';
        setTimeout(() => {
            document.getElementById('scn-progress-wrap').style.display = 'none';
        }, 400);

        if (d.error) { showToast('Scanner error: ' + d.error, 'error'); return; }

        _scanResults = d.results || [];

        // Summary chips
        const sumEl = document.getElementById('scn-summary');
        sumEl.style.display = 'flex';
        _setText('scn-chip-both',   `${d.both_passing}  both ✓`);
        _setText('scn-chip-daily',  `${d.daily_passing}  daily ✓`);
        _setText('scn-chip-weekly', `${d.weekly_passing}  weekly ✓`);
        const noData = (d.no_data || []).length;
        _setText('scn-chip-none',   `${noData}  no local data`);
        _setText('scn-scanned',     `${d.total_scanned} scanned · D≤${dailyT} W≤${weeklyT} · ${d.dma20_passing || 0} Below 20DMA`);
        // Annotate the no-data chip with a tooltip listing affected symbols
        const noneEl = document.getElementById('scn-chip-none');
        if (noneEl && d.no_data && d.no_data.length) {
            noneEl.title = `No local CSV + OMS fetch unavailable for: ${d.no_data.slice(0,20).join(', ')}${d.no_data.length > 20 ? ` … and ${d.no_data.length - 20} more` : ''}`;
        }

        renderScanResults();
    })
    .catch(() => {
        clearInterval(progTimer);
        document.getElementById('scn-progress-wrap').style.display = 'none';
        showToast('Scanner network error', 'error');
    })
    .finally(() => {
        if (runBtn) { runBtn.disabled = false; runBtn.textContent = '▶ Run Scanner'; }
    });
}

// Scanner sort state: 'dwr' | 'wwr' | 'dma'  ·  dir: 1=asc, -1=desc
let _scnSortCol = 'dwr';
let _scnSortDir = 1;  // 1 = asc (most negative first for W%R)

function setScanSort(col) {
    if (_scnSortCol === col) {
        _scnSortDir = -_scnSortDir;
    } else {
        _scnSortCol = col;
        _scnSortDir = 1;
    }
    renderScanResults();
}

function renderScanResults() {
    const only20dma  = document.getElementById('scn-only-20dma')?.checked;
    const usedActive = new Set(_symState.active);
    const usedBnh    = new Set(_symState.bnh);

    // Filter results
    let rows = _scanResults.filter(r => !only20dma || r.below_20dma);

    // Sort by selected column
    rows = rows.slice().sort((a, b) => {
        let av, bv;
        if (_scnSortCol === 'wwr') {
            av = a.weekly_wr != null ? a.weekly_wr : (_scnSortDir > 0 ? 1 : -101);
            bv = b.weekly_wr != null ? b.weekly_wr : (_scnSortDir > 0 ? 1 : -101);
        } else if (_scnSortCol === 'dma') {
            // sort by % vs 20 DMA; nulls to end
            const ap = (a.dma20 != null && a.ltp != null) ? ((a.ltp - a.dma20) / a.dma20) * 100 : (_scnSortDir > 0 ? 999 : -999);
            const bp = (b.dma20 != null && b.ltp != null) ? ((b.ltp - b.dma20) / b.dma20) * 100 : (_scnSortDir > 0 ? 999 : -999);
            av = ap; bv = bp;
        } else { // 'dwr' default
            av = a.daily_wr != null ? a.daily_wr : (_scnSortDir > 0 ? 1 : -101);
            bv = b.daily_wr != null ? b.daily_wr : (_scnSortDir > 0 ? 1 : -101);
        }
        return _scnSortDir * (av - bv);
    });

    // Update header arrows
    ['dwr', 'wwr', 'dma'].forEach(col => {
        const arr = document.getElementById(`scn-arr-${col}`);
        if (!arr) return;
        if (col !== _scnSortCol) { arr.textContent = '↕'; arr.classList.remove('active'); return; }
        arr.textContent = _scnSortDir > 0 ? '↑' : '↓';
        arr.classList.add('active');
    });

    const tbody = document.getElementById('scn-body');
    const resEl = document.getElementById('scanner-results');
    const emEl  = document.getElementById('scn-empty');

    if (!rows.length) {
        resEl.style.display = 'none';
        emEl.style.display  = 'block';
        document.getElementById('scn-add-bar').style.display = 'none';
        return;
    }

    emEl.style.display  = 'none';
    resEl.style.display = 'block';

    tbody.innerHTML = rows.map(r => {
        const isEtf     = NSE_ETF_LIST.includes(r.symbol);
        const typeBadge = isEtf
            ? '<span class="sym-dd-badge etf">ETF</span>'
            : '<span class="sym-dd-badge stk">Stock</span>';

        const dwr = r.daily_wr  != null ? r.daily_wr.toFixed(1)  : '—';
        const wwr = r.weekly_wr != null ? r.weekly_wr.toFixed(1) : '—';
        const ltp = r.ltp       != null ? r.ltp.toFixed(2)       : '—';

        const dwrCls = r.both_ok ? 'scn-wr-both' : r.daily_ok  ? 'scn-wr-daily'  : 'scn-wr-neutral';
        const wwrCls = r.both_ok ? 'scn-wr-both' : r.weekly_ok ? 'scn-wr-weekly' : 'scn-wr-neutral';

        // vs 20 DMA cell — show % distance from DMA (negative = below)
        let dmaHtml;
        if (r.dma20 != null && r.ltp != null) {
            const pct = ((r.ltp - r.dma20) / r.dma20) * 100;
            const pctStr = (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
            const title = `LTP ${r.ltp.toFixed(2)} · 20 DMA ${r.dma20.toFixed(2)}`;
            if (r.below_20dma) {
                dmaHtml = `<span class="scn-dma-below" title="${title}">📉 ${pctStr}</span>`;
            } else {
                dmaHtml = `<span class="scn-dma-above" title="${title}">${pctStr}</span>`;
            }
        } else {
            dmaHtml = '—';
        }

        let sigHtml;
        if (r.both_ok)        sigHtml = '<span class="scn-sig-both">✅ Both</span>';
        else if (r.daily_ok)  sigHtml = '<span class="scn-sig-daily">📉 Daily only</span>';
        else if (r.weekly_ok) sigHtml = '<span class="scn-sig-weekly">📊 Weekly only</span>';
        else                   sigHtml = '<span class="scn-sig-none">—</span>';

        let inStratHtml = '';
        if (usedActive.has(r.symbol)) inStratHtml = '<span class="scn-in-strat scn-in-active">Active</span>';
        else if (usedBnh.has(r.symbol)) inStratHtml = '<span class="scn-in-strat scn-in-bnh">Dip Acc.</span>';

        const rowCls = r.below_20dma ? 'scn-row-both' : r.both_ok ? 'scn-row-daily' : '';
        const alreadyUsed = usedActive.has(r.symbol) || usedBnh.has(r.symbol);

        return `<tr class="${rowCls}" data-symbol="${r.symbol}">
            <td><input type="checkbox" class="scn-row-check" value="${r.symbol}"
                       ${alreadyUsed ? 'disabled title="Already in a strategy"' : ''}
                       onchange="updateScanSelCount()"></td>
            <td><strong>${r.symbol}</strong></td>
            <td>${typeBadge}</td>
            <td>${ltp}</td>
            <td class="${dwrCls}">${dwr}</td>
            <td class="${wwrCls}">${wwr}</td>
            <td>${dmaHtml}</td>
            <td>${sigHtml}</td>
            <td>${inStratHtml}</td>
        </tr>`;
    }).join('');

    updateScanSelCount();
}

function updateScanSelCount() {
    const checks = document.querySelectorAll('.scn-row-check:checked');
    const cnt    = checks.length;
    const addBar = document.getElementById('scn-add-bar');
    const cntEl  = document.getElementById('scn-sel-count');
    if (cntEl) cntEl.textContent = `${cnt} selected`;
    if (addBar) addBar.style.display = cnt > 0 ? 'flex' : 'none';
}

function toggleAllScanRows(checked) {
    document.querySelectorAll('.scn-row-check:not(:disabled)').forEach(cb => {
        cb.checked = checked;
    });
    updateScanSelCount();
}

function clearScanSelection() {
    document.querySelectorAll('.scn-row-check').forEach(cb => { cb.checked = false; });
    const allChk = document.getElementById('scn-check-all');
    if (allChk) allChk.checked = false;
    updateScanSelCount();
}

function addScanSelected(strategy) {
    const checks  = document.querySelectorAll('.scn-row-check:checked');
    if (!checks.length) return;

    const usedActive = new Set(_symState.active);
    const usedBnh    = new Set(_symState.bnh);
    const other      = strategy === 'active' ? usedBnh : usedActive;
    const otherLabel = strategy === 'active' ? 'Dip Accumulator' : 'Active Strategy';
    const thisLabel  = strategy === 'active' ? 'Active Strategy' : 'Dip Accumulator';

    let added = [], dupeThis = [], dupeOther = [];

    checks.forEach(cb => {
        const sym = cb.value;
        if (_symState[strategy].includes(sym)) { dupeThis.push(sym); return; }
        if (other.has(sym))                    { dupeOther.push(sym); return; }
        _symState[strategy].push(sym);
        added.push(sym);
    });

    if (added.length) {
        _renderSymList(strategy);
        _markUnsaved(strategy);
        showToast(`✅ Added ${added.length} symbol${added.length > 1 ? 's' : ''} to ${thisLabel} — remember to Save`, 'success');
    }
    if (dupeOther.length) {
        showToast(`⚠️ ${dupeOther.join(', ')} already in ${otherLabel} — skipped`, 'warning');
    }

    // Re-render to update "In Strategy" column
    clearScanSelection();
    renderScanResults();
}

function _setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
}

// ── Scanner Seed Data ─────────────────────────────────────────────────────────

let _seedPollTimer = null;

function runScannerSeed() {
    // Build the full symbol universe (same as _getScanSymbols but always all lists)
    const syms = new Set();
    NSE_ETF_LIST.forEach(s => syms.add(s));
    if (typeof NSE_NIFTY200_LIST  !== 'undefined') NSE_NIFTY200_LIST.forEach(s  => syms.add(s));
    if (typeof NSE_MIDCAP150_LIST !== 'undefined') NSE_MIDCAP150_LIST.forEach(s => syms.add(s));
    const symbols = [...syms];

    // Open modal immediately
    _openSeedModal(symbols.length);

    fetch('/api/scanner/seed', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ symbols }),
    })
    .then(r => r.json())
    .then(d => {
        if (!d.ok) {
            document.getElementById('seed-prog-label').textContent = 'Error: ' + (d.error || 'Unknown');
            return;
        }
        // Start polling
        _seedPollTimer = setInterval(_pollSeedStatus, 800);
    })
    .catch(err => {
        document.getElementById('seed-prog-label').textContent = 'Network error: ' + err;
    });
}

function _openSeedModal(total) {
    const overlay = document.getElementById('seed-modal-overlay');
    if (overlay) overlay.classList.add('open');
    document.getElementById('seed-prog-fill').style.width  = '0%';
    document.getElementById('seed-prog-label').textContent = `0 / ${total} symbols…`;
    document.getElementById('seed-log').textContent        = '';
    document.getElementById('seed-modal-sub').textContent  =
        `Downloading historical CSVs for ${total} symbols — this may take a few minutes.`;
    const seedBtn = document.getElementById('scn-seed-btn');
    if (seedBtn) { seedBtn.disabled = true; seedBtn.textContent = '⏳ Seeding…'; }
}

function closeSeedModal() {
    const overlay = document.getElementById('seed-modal-overlay');
    if (overlay) overlay.classList.remove('open');
    if (_seedPollTimer) { clearInterval(_seedPollTimer); _seedPollTimer = null; }
    const seedBtn = document.getElementById('scn-seed-btn');
    if (seedBtn) { seedBtn.disabled = false; seedBtn.textContent = '⬇ Seed Data'; }
}

function _pollSeedStatus() {
    fetch('/api/scanner/seed/status')
    .then(r => r.json())
    .then(s => {
        const pct = s.total > 0 ? Math.round((s.done / s.total) * 100) : 0;
        document.getElementById('seed-prog-fill').style.width  = pct + '%';
        document.getElementById('seed-prog-label').textContent =
            `${s.done} / ${s.total}  ·  ✓ ${s.ok}  ⏭ ${s.skipped}  ✗ ${s.failed}` +
            (s.current && s.current !== 'Complete' ? `  ·  ${s.current}` : '');
        // Show log
        const logEl = document.getElementById('seed-log');
        if (logEl && s.log && s.log.length) {
            logEl.textContent = s.log.join('\n');
            logEl.scrollTop   = logEl.scrollHeight;
        }
        if (!s.running) {
            clearInterval(_seedPollTimer);
            _seedPollTimer = null;
            document.getElementById('seed-prog-fill').style.width = '100%';
            document.getElementById('seed-prog-label').textContent =
                `Complete — ✓ ${s.ok} downloaded  ⏭ ${s.skipped} already fresh  ✗ ${s.failed} failed`;
            const seedBtn = document.getElementById('scn-seed-btn');
            if (seedBtn) { seedBtn.disabled = false; seedBtn.textContent = '⬇ Seed Data'; }
        }
    })
    .catch(() => {});
}
