// app.js

document.addEventListener('DOMContentLoaded', () => {

    // ── Tab switching ──────────────────────────────────────────
    const tabBtns = document.querySelectorAll('.tab-btn');
    const tabHoldings = document.getElementById('tab-holdings');
    const tabCross    = document.getElementById('tab-cross');
    const tabSearch   = document.getElementById('tab-search');
    const appHeader   = document.querySelector('.app-header');
    const ytdRankingBar = document.getElementById('ytd-ranking-bar');

    let activeTab = 'holdings';

    tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            tabBtns.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            activeTab = btn.dataset.tab;

            if (activeTab === 'holdings') {
                tabHoldings.style.display = '';
                tabCross.style.display = 'none';
                tabSearch.style.display = 'none';
                appHeader.style.display = '';
                if (ytdRankingBar) ytdRankingBar.style.display = '';
            } else if (activeTab === 'cross') {
                tabHoldings.style.display = 'none';
                tabCross.style.display = '';
                tabSearch.style.display = 'none';
                appHeader.style.display = 'none';
                if (ytdRankingBar) ytdRankingBar.style.display = 'none';
                loadCrossData();
            } else if (activeTab === 'search') {
                tabHoldings.style.display = 'none';
                tabCross.style.display = 'none';
                tabSearch.style.display = '';
                appHeader.style.display = 'none';
                if (ytdRankingBar) ytdRankingBar.style.display = 'none';
                loadCrossData();
            }
        });
    });

    // ── Holdings tab ───────────────────────────────────────────
    const tbody       = document.getElementById('holdings-body');
    const updateBadge = document.getElementById('update-date');
    const thDiffAmount  = document.getElementById('th-diff-amount');
    const thStatus      = document.getElementById('th-status');
    const thDiffShares  = document.getElementById('th-diff-shares');

    // sortMode: 'weight' | 'amount' | 'shares' | 'status'
    let sortMode      = 'weight';
    let diffAmountDir = 1;   // 1=desc, -1=asc
    let diffSharesDir = 1;   // 1=desc, -1=asc
    // status sort cycles: 0=default, 1=positive first (新增→加碼→持平→減碼→出清), -1=negative first
    let statusDir     = 1;
    let globalData    = [];

    // status rank: higher = shown first in positive-first order
    const statusRank = (h) => {
        const prev = h.prevShares ?? 0, curr = h.shares;
        if (prev === 0 && curr > 0) return 4;  // 新增
        if (curr > prev && prev > 0) return 3; // 加碼
        if (curr === prev)           return 2; // 持平
        if (curr < prev && curr > 0) return 1; // 減碼
        if (curr === 0 && prev > 0)  return 0; // 出清
        return 2;
    };

    // badge hidden

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

            const sharesDisplay = (() => {
                const prev = holding.prevShares ?? 0, curr = holding.shares;
                if (prev === 0 || prev === curr) return `<span>${formatNumber(curr)}</span>`;
                const color = curr > prev ? '#ff4d4d' : '#4ade80';
                return `<span style="color:#9ca3af;font-size:0.8em;">${formatNumber(prev)}</span> <span style="color:${color};">→</span> <span style="font-weight:600;">${formatNumber(curr)}</span>`;
            })();

            tr.innerHTML = `
                <td data-label="序號"><span style="display:inline-block;width:30px;height:30px;line-height:30px;text-align:center;border-radius:50%;background:#334155;color:#fff;font-weight:bold;">${holding.rank}</span></td>
                <td data-label="股票"><div class="stock-id">${holding.code}</div><div class="stock-name">${holding.name}</div></td>
                <td data-label="股價" class="align-right stock-price">$${formatNumber(holding.price, 2)}</td>
                <td data-label="股數" class="stock-shares">${sharesDisplay}</td>
                <td data-label="比例" class="align-right">${weightDisplay}</td>
                <td data-label="狀態" class="align-right">${renderStatus(holding)}</td>
                <td data-label="加/減碼股數" class="align-right">${renderDiff(holding.diffShares, 0)}</td>
                <td data-label="加/減碼金額" class="align-right">$${renderDiff(holding.diffAmount, 0)}</td>
            `;
            tbody.appendChild(tr);
        });
    };

    const resetHeaderIcons = () => {
        thDiffAmount.innerHTML  = '<i class="fa-solid fa-sack-dollar"></i> 加/減碼金額 <i class="fa-solid fa-sort" style="opacity:0.3"></i>';
        thStatus.innerHTML      = '<i class="fa-solid fa-tag"></i> 狀態 <i class="fa-solid fa-sort" style="opacity:0.3"></i>';
        thDiffShares.innerHTML  = '<i class="fa-solid fa-arrow-trend-up"></i> 加/減碼股數 <i class="fa-solid fa-sort" style="opacity:0.3"></i>';
    };

    const applySortAndRender = () => {
        let sorted = [...globalData];
        resetHeaderIcons();

        if (sortMode === 'amount') {
            sorted.sort((a, b) => diffAmountDir === 1 ? b.diffAmount - a.diffAmount : a.diffAmount - b.diffAmount);
            thDiffAmount.innerHTML = `<i class="fa-solid fa-sack-dollar"></i> 加/減碼金額 <i class="fa-solid fa-sort-${diffAmountDir === 1 ? 'down' : 'up'}"></i>`;
        } else if (sortMode === 'shares') {
            sorted.sort((a, b) => diffSharesDir === 1 ? b.diffShares - a.diffShares : a.diffShares - b.diffShares);
            thDiffShares.innerHTML = `<i class="fa-solid fa-arrow-trend-up"></i> 加/減碼股數 <i class="fa-solid fa-sort-${diffSharesDir === 1 ? 'down' : 'up'}"></i>`;
        } else if (sortMode === 'status') {
            sorted.sort((a, b) => statusDir === 1 ? statusRank(b) - statusRank(a) : statusRank(a) - statusRank(b));
            thStatus.innerHTML = `<i class="fa-solid fa-tag"></i> 狀態 <i class="fa-solid fa-sort-${statusDir === 1 ? 'down' : 'up'}"></i>`;
        } else {
            sorted.sort((a, b) => b.todayWeight - a.todayWeight);
        }
        renderTable(sorted);
    };

    thDiffAmount.addEventListener('click', () => {
        if (sortMode === 'amount') {
            if (diffAmountDir === 1) { diffAmountDir = -1; }
            else { sortMode = 'weight'; diffAmountDir = 1; }
        } else {
            sortMode = 'amount'; diffAmountDir = 1;
        }
        applySortAndRender();
    });

    thDiffShares.addEventListener('click', () => {
        if (sortMode === 'shares') {
            if (diffSharesDir === 1) { diffSharesDir = -1; }
            else { sortMode = 'weight'; diffSharesDir = 1; }
        } else {
            sortMode = 'shares'; diffSharesDir = 1;
        }
        applySortAndRender();
    });

    thStatus.addEventListener('click', () => {
        if (sortMode === 'status') {
            if (statusDir === 1) { statusDir = -1; }
            else { sortMode = 'weight'; statusDir = 1; }
        } else {
            sortMode = 'status'; statusDir = 1;
        }
        applySortAndRender();
    });

    const etfSelector = document.getElementById('etf-selector');

    const loadData = (etfId) => {
        sortMode = 'weight';
        resetHeaderIcons();
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:2rem;">載入中，請稍候...</td></tr>';
        fetch(`data_${etfId}.json`)
            .then(r => { if (!r.ok) throw new Error('無資料檔'); return r.json(); })
            .then(data => {
                const meta = data.meta;
                globalData = data.holdings;
                const elSubtitle = document.getElementById('header-subtitle');
                if (elSubtitle) {
                    const priceDateStr = meta.priceDate
                        ? `<span style="color:#6b7280;font-size:0.82em;margin-left:0.3em;">(${meta.priceDate})</span>`
                        : '';
                    const priceStr = meta.etfPrice
                        ? ` &nbsp;|&nbsp; <i class="fa-solid fa-dollar-sign"></i> 股價：<span style="color:#60a5fa;font-weight:bold;">${Number(meta.etfPrice).toFixed(2)}</span>${priceDateStr}`
                        : '';
                    elSubtitle.innerHTML = `<i class="fa-solid fa-user-tie"></i> 經理人：${meta.manager}${priceStr} &nbsp;|&nbsp; <i class="fa-solid fa-chart-line"></i> 今年以來(YTD)績效：<span style="color:${meta.ytd >= 0 ? '#ff4d4d' : '#4ade80'};font-weight:bold;">${meta.ytd > 0 ? '+' : ''}${meta.ytd}%</span>`;
                }
                // badge removed
                const elLastUpdate = document.getElementById('last-update-time');
                if (elLastUpdate && meta.lastUpdate) elLastUpdate.textContent = `最後更新時間：${meta.lastUpdate}`;

                // ETF 規模資訊 (總股數 & 市值)
                const elScale = document.getElementById('etf-scale-info');
                if (elScale && meta.totalShares != null) {
                    const fmtZhang = n => n >= 10000
                        ? `${(n / 10000).toFixed(1)}萬張`
                        : `${n.toLocaleString()}張`;
                    const sharesNow = meta.totalShares || 0;
                    const sharesPrev = meta.prevTotalShares || 0;
                    const sharesDiff = sharesNow - sharesPrev;
                    let sharesDiffStr = '';
                    if (sharesDiff !== 0 && sharesPrev > 0) {
                        const arrow = sharesDiff > 0 ? '↑' : '↓';
                        const color = sharesDiff > 0 ? '#ff4d4d' : '#4ade80';
                        sharesDiffStr = ` <span style="color:${color};font-weight:700;">(${arrow}${fmtZhang(Math.abs(sharesDiff))})</span>`;
                    }

                    const capNow = meta.totalMarketCap || 0;
                    const capPrev = meta.prevTotalMarketCap || 0;
                    const capDiff = capNow - capPrev;
                    let capDiffStr = '';
                    if (Math.abs(capDiff) >= 0.01 && capPrev > 0) {
                        const arrow = capDiff > 0 ? '↑' : '↓';
                        const color = capDiff > 0 ? '#ff4d4d' : '#4ade80';
                        capDiffStr = ` <span style="color:${color};font-weight:700;">(${arrow}${Math.abs(capDiff).toFixed(2)}億)</span>`;
                    }

                    elScale.innerHTML = `<i class="fa-solid fa-layer-group"></i> 基金規模：${fmtZhang(sharesNow)}${sharesDiffStr} &nbsp;|&nbsp; <i class="fa-solid fa-coins"></i> 市值：${capNow.toFixed(2)}億${capDiffStr}`;
                    elScale.style.display = '';
                } else if (elScale) {
                    elScale.style.display = 'none';
                }
                applySortAndRender();
            })
            .catch(err => {
                console.error(err);
                tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;color:#ef4444;padding:2rem;">無法載入 ${etfId} 的持股資料。</td></tr>`;
            });
    };

    etfSelector.addEventListener('change', e => {
        loadData(e.target.value);
        document.querySelectorAll('.ytd-item').forEach(el => {
            el.classList.toggle('ytd-item-active', el.dataset.etf === e.target.value);
        });
    });
    loadData(etfSelector.value);

    // ── YTD Ranking ────────────────────────────────────────────
    const ALL_ETFS = [
        { id: '00981A', name: '統一台股增長' },
        { id: '00980A', name: '野村智慧優選' },
        { id: '00985A', name: '野村台灣50' },
        { id: '00991A', name: '復華未來50' },
        { id: '00992A', name: '群益科技創新' },
        { id: '00982A', name: '群益台灣強棒' },
        { id: '00987A', name: '台新台灣優勢成長' },
        { id: '00993A', name: '主動安聯台灣' },
        { id: '00995A', name: '主動中信台灣卓越' },
    ];

    const loadYtdRanking = () => {
        fetch('data_index.json')
            .then(r => r.ok ? r.json() : null)
            .then(idx => {
                if (idx?.twii_ytd != null) {
                    const val = parseFloat(idx.twii_ytd);
                    const sign = val >= 0 ? '+' : '';
                    const color = val >= 0 ? '#ff4d4d' : '#4ade80';
                    document.getElementById('twii-ytd-display').innerHTML =
                        `(加權指數績效 <span style="color:${color};font-weight:700">${sign}${val.toFixed(2)}%</span>)`;
                }
            })
            .catch(() => {});

        Promise.all(ALL_ETFS.map(etf =>
            fetch(`data_${etf.id}.json`)
                .then(r => r.ok ? r.json() : null)
                .then(data => data ? { id: etf.id, name: etf.name, ytd: parseFloat(data.meta.ytd), etfPrice: data.meta.etfPrice } : null)
                .catch(() => null)
        )).then(results => {
            const valid = results.filter(Boolean)
                .sort((a, b) => b.ytd - a.ytd);

            const rankStyles = [
                { bg: 'linear-gradient(135deg,#f59e0b,#d97706)', color: '#fff', shadow: '0 2px 8px rgba(245,158,11,0.5)' },
                { bg: 'linear-gradient(135deg,#94a3b8,#64748b)', color: '#fff', shadow: '0 2px 8px rgba(148,163,184,0.4)' },
                { bg: 'linear-gradient(135deg,#cd7c2f,#a16207)', color: '#fff', shadow: '0 2px 8px rgba(205,124,47,0.4)' },
            ];
            const list = document.getElementById('ytd-ranking-list');
            list.innerHTML = valid.map((etf, i) => {
                const sign = etf.ytd >= 0 ? '+' : '';
                const color = etf.ytd >= 0 ? '#ff4d4d' : '#4ade80';
                const rs = rankStyles[i];
                const rankBadge = rs
                    ? `<span class="ytd-rank-badge" style="background:${rs.bg};color:${rs.color};box-shadow:${rs.shadow};">${i + 1}</span>`
                    : `<span class="ytd-rank-num">${i + 1}</span>`;
                const isTop3 = i < 3 ? 'ytd-item-top3' : '';
                return `
                    <div class="ytd-item ${isTop3} ${etf.id === etfSelector.value ? 'ytd-item-active' : ''}" data-etf="${etf.id}">
                        ${rankBadge}
                        <span class="ytd-etf-id">${etf.id}</span>
                        <span class="ytd-etf-name">${etf.name}</span>
                        <span class="ytd-value" style="color:${color}">${sign}${etf.ytd.toFixed(2)}%</span>
                    </div>`;
            }).join('');

            // Click to switch ETF
            list.querySelectorAll('.ytd-item').forEach(el => {
                el.addEventListener('click', () => {
                    const id = el.dataset.etf;
                    etfSelector.value = id;
                    loadData(id);
                    list.querySelectorAll('.ytd-item').forEach(e => e.classList.remove('ytd-item-active'));
                    el.classList.add('ytd-item-active');
                });
            });
        });
    };

    loadYtdRanking();

    // ── Cross-compare tab ──────────────────────────────────────
    const ETF_LIST = [
        { id: '00981A', name: '統一台股增長' },
        { id: '00980A', name: '野村智慧優選' },
        { id: '00985A', name: '野村台灣50' },
        { id: '00991A', name: '復華未來50' },
        { id: '00992A', name: '群益科技創新' },
        { id: '00982A', name: '群益台灣強棒' },
        { id: '00987A', name: '台新台灣優勢成長' },
        { id: '00993A', name: '主動安聯台灣' },
        { id: '00995A', name: '主動中信台灣卓越' },
    ];

    let crossSortAsc = false;
    let crossData = [];
    let crossLoaded = false;
    let globalStockMap = new Map();

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
                <td data-label="序號"><span style="display:inline-block;width:30px;height:30px;line-height:30px;text-align:center;border-radius:50%;background:#334155;color:#fff;font-weight:bold;">${index + 1}</span></td>
                <td data-label="股票"><div class="stock-id">${row.code}</div><div class="stock-name">${row.name}</div></td>
                <td data-label="持有 ETF 與比例"><div class="etf-tags">${etfTags}</div></td>
                <td data-label="持有 ETF 數" class="align-right">${countBadge}</td>
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
            globalStockMap.clear();
            valid.forEach(({ etf, holdings }) => {
                holdings.filter(h => h.shares > 0).forEach(h => {
                    if (!globalStockMap.has(h.code)) {
                        globalStockMap.set(h.code, { code: h.code, name: h.name, etfs: [] });
                    }
                    globalStockMap.get(h.code).etfs.push({
                        etfId: etf.id,
                        etfName: etf.name,
                        weight: h.todayWeight,
                    });
                });
            });

            crossData = Array.from(globalStockMap.values())
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
            if (activeTab === 'search') handleSearch();
        });
    };

    // ── Search Tab ──────────────────────────────────────────────
    const searchInput = document.getElementById('stock-search-input');
    const clearBtn = document.getElementById('clear-search-btn');
    const searchStatus = document.getElementById('search-status');
    const searchEmptyState = document.getElementById('search-empty-state');
    const searchTable = document.getElementById('search-table');
    const searchBody = document.getElementById('search-body');
    const searchResultTitle = document.getElementById('search-result-title');

    const renderSearchResults = (stock) => {
        if (!stock) {
            searchEmptyState.style.display = 'block';
            searchTable.style.display = 'none';
            searchResultTitle.textContent = '';
            searchStatus.textContent = '查無相符股票，請重新輸入代號或完整名稱。';
            return;
        }

        searchEmptyState.style.display = 'none';
        searchTable.style.display = '';
        searchResultTitle.textContent = `- ${stock.code} ${stock.name}`;
        searchStatus.innerHTML = `找到 <strong style="color:var(--text-primary)">${stock.etfs.length}</strong> 檔 ETF 持有此股票`;

        const sortedEtfs = [...stock.etfs].sort((a, b) => b.weight - a.weight);
        searchBody.innerHTML = '';

        sortedEtfs.forEach((etf, index) => {
            const tr = document.createElement('tr');
            tr.style.animation = `fadeInUp 0.3s cubic-bezier(0.16,1,0.3,1) ${Math.min(0.05 + index * 0.015, 0.8)}s forwards`;
            tr.style.opacity = '0';
            tr.style.transform = 'translateY(10px)';

            tr.innerHTML = `
                <td data-label="序號"><span style="display:inline-block;width:30px;height:30px;line-height:30px;text-align:center;border-radius:50%;background:#334155;color:#fff;font-weight:bold;">${index + 1}</span></td>
                <td data-label="持有 ETF"><div class="stock-id">${etf.etfId}</div><div class="stock-name">${etf.etfName}</div></td>
                <td data-label="權重佔比" class="align-right"><span class="weight-pill">${etf.weight}%</span></td>
            `;
            searchBody.appendChild(tr);
        });
    };

    const handleSearch = () => {
        if (!searchInput) return;
        const query = searchInput.value.trim().toLowerCase();
        if (!query) {
            clearBtn.style.display = 'none';
            searchEmptyState.style.display = 'block';
            searchTable.style.display = 'none';
            searchResultTitle.textContent = '';
            searchStatus.textContent = '準備就緒，輸入關鍵字開始搜尋。';
            return;
        }
        clearBtn.style.display = 'block';

        if (!crossLoaded) {
            searchStatus.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 正在載入資料庫，請稍候...';
            return;
        }

        if (globalStockMap.has(query)) {
            renderSearchResults(globalStockMap.get(query));
            return;
        }

        let foundStock = null;
        for (const stock of globalStockMap.values()) {
            if (stock.code.includes(query) || stock.name.toLowerCase().includes(query)) {
                foundStock = stock;
                if (stock.name === query) break;
            }
        }
        renderSearchResults(foundStock);
    };

    if (searchInput) {
        searchInput.addEventListener('input', handleSearch);
        clearBtn.addEventListener('click', () => {
            searchInput.value = '';
            handleSearch();
            searchInput.focus();
        });
    }
});
