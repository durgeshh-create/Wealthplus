// Wealth++ Algo Dashboard — Tab Navigation
// Handles tab switching and persists active tab in sessionStorage

(function () {
    const TAB_KEY = 'wpp_active_tab';

    function switchTab(tabName) {
        // Hide all panes
        document.querySelectorAll('.tab-pane').forEach(el => el.classList.remove('active'));
        // Deactivate all buttons
        document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));

        // Activate target pane
        const pane = document.getElementById('tab-' + tabName);
        if (pane) pane.classList.add('active');

        // Activate matching button
        const btn = document.querySelector('.tab-btn[data-tab="' + tabName + '"]');
        if (btn) btn.classList.add('active');

        // Persist selection
        try { sessionStorage.setItem(TAB_KEY, tabName); } catch (_) {}

        // Trigger lazy loads when tabs become visible
        if (tabName === 'portfolio') {
            syncHoldingsToPortfolioTab();
            // Force fresh Available Cash & Margin fetch — reset throttle so
            // refreshFundsCards() hits Zerodha immediately instead of using
            // a potentially stale cached value from up to 5 min ago.
            window._lastFundsFetch = 0;
            if (typeof refreshFundsCards === 'function') {
                setTimeout(refreshFundsCards, 50);
            }
        }
        if (tabName === 'activity') {
            if (typeof loadTransactions === 'function') loadTransactions();
            if (typeof loadLogs === 'function') loadLogs();
            if (typeof updateSignalSummary === 'function') updateSignalSummary();
        }
        if (tabName === 'market') {
            if (typeof loadMarketData === 'function') loadMarketData();
        }
        if (tabName === 'intraday') {
            if (typeof startBnHRefresh === 'function') startBnHRefresh();
        }
    }

    // Mirror holdings data from the modal body into the inline portfolio table
    function syncHoldingsToPortfolioTab() {
        const src = document.getElementById('holdings-modal-body') ||
                    document.getElementById('holdings-modal-body-legacy');
        const dst = document.getElementById('holdings-modal-body');
        if (!src || !dst || src === dst) return;
        dst.innerHTML = src.innerHTML;

        // Sync meta elements
        const metaTime = document.getElementById('holdings-live-time-modal');
        const inlineTime = document.getElementById('holdings-live-time');
        if (metaTime && inlineTime) inlineTime.textContent = metaTime.textContent;

        const metaProfit = document.getElementById('holdings-meta-profit-modal');
        const inlineProfit = document.getElementById('holdings-meta-profit');
        if (metaProfit && inlineProfit) inlineProfit.textContent = metaProfit.textContent;
    }

    // Restore previously selected tab (or default to 'controls')
    function restoreTab() {
        let saved;
        try { saved = sessionStorage.getItem(TAB_KEY); } catch (_) {}
        switchTab(saved || 'controls');
    }

    // Expose globally for onclick handlers in HTML
    window.switchTab = switchTab;

    // Patch openHoldingsModal so it switches to portfolio tab instead
    window.openHoldingsModal = function () {
        switchTab('portfolio');
    };
    window.closeHoldingsModal = function () { /* no-op — no modal */ };

    // Restore on DOMContentLoaded
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', restoreTab);
    } else {
        restoreTab();
    }

    // Pulse the market tab indicator whenever market data updates
    const origLoadMarket = window.loadMarketData;
    if (typeof origLoadMarket === 'function') {
        window.loadMarketData = function () {
            const pulse = document.getElementById('market-pulse');
            if (pulse) { pulse.style.opacity = '1'; setTimeout(() => { pulse.style.opacity = '0.4'; }, 400); }
            return origLoadMarket.apply(this, arguments);
        };
    }
})();
