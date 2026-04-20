// app.js

document.addEventListener('DOMContentLoaded', () => {

    // ── Tab switching ──────────────────────────────────────────
    const tabBtns = document.querySelectorAll('.tab-btn');
    const tabHoldings = document.getElementById('tab-holdings');
    const tabCross    = document.getElementById('tab-cross');
    const etfSelectWrapper = document.getElementById('etf-select-wrapper');
    const etfSwitchHint    = document.getElementById('etf-switch-hint');
    const headerSubtitle   = document.getElementById('header-subtitle');

    let activeTab = 'holdings';

    tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            tabBtns.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            activeTab = btn.dataset.tab;

            if (activeTab === 'holdings') {
                tabHoldings.style.display = '';
                tabCross.style.display = 'none';
                etfSelectWrapper.style.display = '';
                etfSwitchHint.style.display = '';
                headerSubtitle.style.display = '';
            } else {
                tabHoldings.style.display = 'none';
                tabCross.style.display = '';
                etfSelectWrapper.style.display = 'none';
                etfSwitchHint.style.display = 'none';
                headerSubtitle.style.display = 'none';
                loadCrossData();
            }
        });
    });

    // ── Holdings tab ───────────────────────────────────────────
    const tbody       = document.getElementById('holdings-body');
    const updateBadge = document.getElementById('update-date');
    const thDiffAmount = document.getElementById('th-diff-amount');

    let diffSortState = 0;
    let globalData = [];

    updateBadge.textContent = '最新交易日差異比較 (...)';

    const formatNumber = (num, decimals = 0) =>
        Number(Math.abs(num)).toLocaleString('zh-TW', {
            minimumFractionDigits: decimals,
            maximumFractionDigits: decimals,
        });

    const renderDiff = (num, decimals = 0) => {
        const absStr = formatNumber(num, decimals);
        if (num > 0) return `<span style="color:#ff4d4d;font-weight:bold;">+${absStr}</span>`;
        if (num < 0) return `<span style="color:#4ade80;font-weight:bold;">-${absStr}</span>`;
        return `<span style="color:#6b7280;">0</span>`;
    };

    const renderStatus = (holding) => {
        const prev = holding.prevShares ?? null;
        const curr = holding.shares;
        let label = '-', style = 'color:#6b7280;';
        if (prev === null || prev === undefined) { /* keep default */ }
        else if (prev === 0 && curr > 0)  { label = '新增'; style = 'color:#a78bfa;font-weight:bold;'; }
        else if (prev > 0 && curr === 0)  { label = '出清'; style = 'color:#f97316;font-weight:bold;'; }
        else if (curr > prev)             { label = '加碼'; style = 'color:#ff4d4d;font-weight:bold;'; }
        else if (curr < prev)             { label = '減碼'; style = 'color:#4ade80;font-weight:bold;'; }
        return `<span style="${style}">${label}</span>`;
    };

    const renderTable = (holdings) => {
        tbody.innerHTML = '';
        holdings.forEach((holding, index) => {
            const tr = document.createElement('tr');
            tr.style.animation = `fadeInUp 0.3s cubic-bezier(0.16,1,0.3,1) ${Math.min(0.1 + index * 0.02, 1)}s forwards`;
            tr.style.opacity = '0';
            tr.style.transform = 'translateY(10px)';

            const weightDisplay = (() => {
                const prev = holding.yestWeight, curr = holding.todayWeight;
                if (!curr && curr !== 0) return '-';
                if (!prev && prev !== 0) return `<span class="weight-pill">${curr}%</span>`;
                if (prev === curr)       return `<span class="weight-pill">${curr}%</span>`;
                const color = curr > prev ? '#ff4d4d' : '#4ade80';
                return `<span style="color:#9ca3af;font-size:0.8em;">${prev}%</span> <span style="color:${color};">→</span> <span class="weight-pill">${curr}%</span>`;
            })();

            tr.innerHTML = `
                <td><span style="display:inline-block;width:30px;height:30px;line-height:30px;text-align:center;border-radius:50%;background:#334155;color:#fff;font-weight:bold;">${holding.rank}</span></td>
                <td><div class="stock-id">${holding.code}</div><div class="stock-name">${holding.name}</div></td>
                <td class="stock-shares">${formatNumber(holding.shares)}</td>
                <td class="align-right stock-price">$${formatNumber(holding.price, 2)}</td>
                <td class="align-right">${weightDisplay}</td>
                <td class="align-right">${renderStatus(holding)}</td>
                <td class="align-right">${renderDiff(holding.diffShares, 0)}</td>
                <td class="align-right">$${renderDiff(holding.diffAmount, 0)}</td>
            `;
            tbody.appendChild(tr);
        });
    };

    const applySortAndRender = () => {
        let sorted = [...globalData];
        if (diffSortState === 1) {
            sorted.sort((a, b) => b.diffAmount - a.diffAmount);
            thDiffAmount.innerHTML = '<i class="fa-solid fa-sack-dollar"></i> 加/減碼金額 <i class="fa-solid fa-sort-down"></i>';
        } else if (diffSortState === -1) {
            sorted.sort((a, b) => a.diffAmount - b.diffAmount);
            thDiffAmount.innerHTML = '<i class="fa-solid fa-sack-dollar"></i> 加/減碼金額 <i class="fa-solid fa-sort-up"></i>';
        } else {
            sorted.sort((a, b) => b.todayWeight - a.todayWeight);
            thDiffAmount.innerHTML = '<i class="fa-solid fa-sack-dollar"></i> 加/減碼金額 <i class="fa-solid fa-sort" style="opacity:0.3"></i>';
        }
        renderTable(sorted);
    };

    thDiffAmount.addEventListener('click', () => {
        diffSortState = diffSortState === 0 ? 1 : diffSortState === 1 ? -1 : 0;
        applySortAndRender();
    });

    const etfSelector = document.getElementById('etf-selector');

    const loadData = (etfId) => {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:2rem;">載入中，請稍候...</td></tr>';
        fetch(`data_${etfId}.json`)
            .then(r => { if (!r.ok) throw new Error('無資料檔'); return r.json(); })
            .then(data => {
                const meta = data.meta;
                globalData = data.holdings;
                const elSubtitle = document.getElementById('header-subtitle');
                if (elSubtitle) {
                    const priceStr = meta.etfPrice
                        ? ` &nbsp;|&nbsp; <i class="fa-solid fa-dollar-sign"></i> 股價：<span style="color:#60a5fa;font-weight:bold;">${Number(meta.etfPrice).toFixed(2)}</span>`
                        : '';
                    elSubtitle.innerHTML = `<i class="fa-solid fa-user-tie"></i> 經理人：${meta.manager}${priceStr} &nbsp;|&nbsp; <i class="fa-solid fa-chart-line"></i> 今年以來(YTD)績效：<span style="color:${meta.ytd >= 0 ? '#ff4d4d' : '#4ade80'};font-weight:bold;">${meta.ytd > 0 ? '+' : ''}${meta.ytd}%</span>`;
                }
                if (meta.dataDate) updateBadge.textContent = `最新交易日差異比較 (${meta.dataDate})`;
                const elLastUpdate = document.getElementById('last-update-time');
                if (elLastUpdate && meta.lastUpdate) elLastUpdate.textContent = `最後更新時間：${meta.lastUpdate}`;
                applySortAndRender();
            })
            .catch(err => {
                console.error(err);
                tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;color:#ef4444;padding:2rem;">無法載入 ${etfId} 的持股資料。</td></tr>`;
            });
    };

    etfSelector.addEventListener('change', e => loadData(e.target.value));
    loadData(etfSelector.value);

    // ── Cross-compare tab ──────────────────────────────────────
    const ETF_LIST = [
        { id: '00981A', name: '統一台股增長' },
        { id: '00992A', name: '群益科技創新' },
        { id: '00982A', name: '群益台灣強棒' },
    ];

    let crossSortAsc = false;
    let crossData = [];
    let crossLoaded = false;

    const thCrossCount = document.getElementById('th-cross-etf-count');
    thCrossCount.addEventListener('click', () => {
        crossSortAsc = !crossSortAsc;
        thCrossCount.innerHTML = `<i class="fa-solid fa-hashtag"></i> 持有 ETF 數 <i class="fa-solid fa-sort-${crossSortAsc ? 'up' : 'down'}"></i>`;
        renderCrossTable(crossData);
    });

    const renderCrossTable = (rows) => {
        const crossBody = document.getElementById('cross-body');
        const sorted = [...rows].sort((a, b) =>
            crossSortAsc ? a.etfs.length - b.etfs.length : b.etfs.length - a.etfs.length
        );
        crossBody.innerHTML = '';
        sorted.forEach((row, index) => {
            const tr = document.createElement('tr');
            tr.style.animation = `fadeInUp 0.3s cubic-bezier(0.16,1,0.3,1) ${Math.min(0.05 + index * 0.015, 0.8)}s forwards`;
            tr.style.opacity = '0';
            tr.style.transform = 'translateY(10px)';

            const etfTags = row.etfs.map(e => `
                <span class="etf-tag">
                    <span class="etf-tag-id">${e.etfId}</span>
                    <span class="etf-tag-name">${e.etfName}</span>
                    <span class="etf-tag-weight">${e.weight}%</span>
                </span>`).join('');

            const countBadge = row.etfs.length >= 2
                ? `<span class="cross-count-badge cross-count-multi">${row.etfs.length}</span>`
                : `<span class="cross-count-badge cross-count-single">${row.etfs.length}</span>`;

            tr.innerHTML = `
                <td><span style="display:inline-block;width:30px;height:30px;line-height:30px;text-align:center;border-radius:50%;background:#334155;color:#fff;font-weight:bold;">${index + 1}</span></td>
                <td><div class="stock-id">${row.code}</div><div class="stock-name">${row.name}</div></td>
                <td><div class="etf-tags">${etfTags}</div></td>
                <td class="align-right">${countBadge}</td>
            `;
            crossBody.appendChild(tr);
        });
    };

    const loadCrossData = () => {
        if (crossLoaded) return;
        const crossBody = document.getElementById('cross-body');
        crossBody.innerHTML = '<tr><td colspan="4" style="text-align:center;padding:2rem;">載入中，請稍候...</td></tr>';

        Promise.all(ETF_LIST.map(etf =>
            fetch(`data_${etf.id}.json`)
                .then(r => r.ok ? r.json() : null)
                .then(data => data ? { etf, holdings: data.holdings, meta: data.meta } : null)
        )).then(results => {
            const valid = results.filter(Boolean);

            // Build code → { code, name, etfs[] } map
            const stockMap = new Map();
            valid.forEach(({ etf, holdings }) => {
                holdings.filter(h => h.shares > 0).forEach(h => {
                    if (!stockMap.has(h.code)) {
                        stockMap.set(h.code, { code: h.code, name: h.name, etfs: [] });
                    }
                    stockMap.get(h.code).etfs.push({
                        etfId: etf.id,
                        etfName: etf.name,
                        weight: h.todayWeight,
                    });
                });
            });

            crossData = Array.from(stockMap.values())
                .filter(s => s.etfs.length >= 2)
                .sort((a, b) => b.etfs.length - a.etfs.length || b.etfs[0].weight - a.etfs[0].weight);

            // Update badge and timestamp
            const crossBadge = document.getElementById('cross-badge');
            const multiCount = crossData.filter(s => s.etfs.length >= 2).length;
            crossBadge.textContent = `共 ${multiCount} 檔重複持有`;

            const dates = valid.map(v => v.meta.lastUpdate).filter(Boolean).sort();
            const elCrossUpdate = document.getElementById('cross-update-time');
            if (elCrossUpdate && dates.length) elCrossUpdate.textContent = `資料更新時間：${dates[dates.length - 1]}`;

            crossLoaded = true;
            renderCrossTable(crossData);
        });
    };
});
