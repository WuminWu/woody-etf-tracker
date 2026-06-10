// app.js

document.addEventListener('DOMContentLoaded', () => {

    // ── Tab switching ──────────────────────────────────────────
    const tabBtns = document.querySelectorAll('.tab-btn');
    const tabHoldings = document.getElementById('tab-holdings');
    const tabCross    = document.getElementById('tab-cross');
    const tabSearch   = document.getElementById('tab-search');
    const tabCommon   = document.getElementById('tab-common');
    const appHeader   = document.querySelector('.app-header');
    const ytdRankingBar = document.getElementById('ytd-ranking-bar');

    let activeTab = 'holdings';

    tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            tabBtns.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            activeTab = btn.dataset.tab;

            const hideAll = () => {
                tabHoldings.style.display = 'none';
                tabCross.style.display = 'none';
                tabSearch.style.display = 'none';
                if (tabCommon) tabCommon.style.display = 'none';
            };
            if (activeTab === 'holdings') {
                hideAll();
                tabHoldings.style.display = '';
                appHeader.style.display = '';
                if (ytdRankingBar) ytdRankingBar.style.display = '';
            } else if (activeTab === 'cross') {
                hideAll();
                tabCross.style.display = '';
                appHeader.style.display = 'none';
                if (ytdRankingBar) ytdRankingBar.style.display = '';
                loadCrossData(true);   // 強制重抓最新持股
            } else if (activeTab === 'search') {
                hideAll();
                tabSearch.style.display = '';
                appHeader.style.display = 'none';
                if (ytdRankingBar) ytdRankingBar.style.display = '';
                loadCrossData(true);   // 強制重抓最新持股
            } else if (activeTab === 'common') {
                hideAll();
                tabCommon.style.display = '';
                appHeader.style.display = 'none';
                if (ytdRankingBar) ytdRankingBar.style.display = '';
                loadCommonActions(true);   // 每次切換到此 tab 都強制重新抓
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
    let currentEtfId  = null;
    let currentDate   = 'latest';      // 'latest' or 'YYYY-MM-DD'
    let manifest      = null;
    let manifestPromise = null;
    let historyData   = null;
    let historyPromise = null;

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

        // ── 現金及其他 row（序號 0，固定在第一列）──────────────
        const cashWeight = Math.max(0, parseFloat(
            (100 - holdings.filter(h => h.shares > 0)
                .reduce((s, h) => s + (h.todayWeight || 0), 0)).toFixed(2)
        ));
        const prevCashWeight = Math.max(0, parseFloat(
            (100 - holdings.reduce((s, h) => s + (h.yestWeight || 0), 0)).toFixed(2)
        ));
        if (cashWeight > 0.01 || prevCashWeight > 0.01) {
            const cashWeightDisplay = (() => {
                if (!prevCashWeight || Math.abs(prevCashWeight - cashWeight) < 0.01)
                    return `<span class="cash-weight-pill">${cashWeight.toFixed(2)}%</span>`;
                const diff = cashWeight - prevCashWeight;
                const arrow = `<span style="color:#6b7280;">→</span>`;
                return `<span style="color:#9ca3af;font-size:0.8em;">${prevCashWeight.toFixed(2)}%</span> ${arrow} <span class="cash-weight-pill">${cashWeight.toFixed(2)}%</span> <span style="color:${diff > 0 ? '#60a5fa' : '#f59e0b'};font-size:0.8em;">(${diff > 0 ? '+' : ''}${diff.toFixed(2)}%)</span>`;
            })();
            const cashTr = document.createElement('tr');
            cashTr.className = 'cash-row';
            cashTr.innerHTML = `
                <td data-label="序號"><span style="display:inline-block;width:30px;height:30px;line-height:30px;text-align:center;border-radius:50%;background:#1e293b;color:#6b7280;font-weight:bold;">0</span></td>
                <td data-label="股票"><div class="stock-id" style="color:#60a5fa;">💰</div><div class="stock-name" style="color:#9ca3af;">現金及其他</div></td>
                <td data-label="股價" class="align-right stock-price" style="color:#4b5563;">—</td>
                <td data-label="股數" class="stock-shares" style="color:#4b5563;">—</td>
                <td data-label="比例" class="align-right">${cashWeightDisplay}</td>
                <td data-label="狀態" class="align-right"><span style="color:#4b5563;">—</span></td>
                <td data-label="加/減碼股數" class="align-right"><span style="color:#4b5563;">—</span></td>
                <td data-label="加/減碼金額" class="align-right"><span style="color:#4b5563;">—</span></td>
            `;
            tbody.appendChild(cashTr);
        }

        holdings.forEach((holding, index) => {
            const tr = document.createElement('tr');
            tr.className = 'clickable-stock';
            tr.dataset.code = holding.code;
            tr.dataset.name = holding.name;
            tr.addEventListener('click', (e) => {
                console.log('[ETF Tracker] row clicked:', holding.code, holding.name, 'currentEtfId=', currentEtfId);
                openHistoryModal(holding.code, holding.name);
            });
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

            const priceDisplay = (() => {
                const prev = holding.prevPrice || 0;
                const curr = holding.price;
                if (!prev || prev === curr) return `$${formatNumber(curr, 2)}`;
                const pct = (curr - prev) / prev * 100;
                const pctSign = pct >= 0 ? '+' : '';
                const color = pct >= 0 ? '#ff4d4d' : '#4ade80';
                return `<span style="color:#9ca3af;font-size:0.8em;">${formatNumber(prev, 2)}</span> <span style="color:${color};">→</span> <span style="font-weight:600;">$${formatNumber(curr, 2)}</span> <span style="color:${color};font-size:0.8em;">(${pctSign}${pct.toFixed(2)}%)</span>`;
            })();

            tr.innerHTML = `
                <td data-label="序號"><span style="display:inline-block;width:30px;height:30px;line-height:30px;text-align:center;border-radius:50%;background:#334155;color:#fff;font-weight:bold;">${holding.rank}</span></td>
                <td data-label="股票"><div class="stock-id">${holding.code}</div><div class="stock-name">${holding.name}</div></td>
                <td data-label="股價" class="align-right stock-price">${priceDisplay}</td>
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

    // ── ETF 配息設定（依各基金公開說明書）──────────────────────
    // distMonths: 除息月份陣列  distDay: 預計除息日（超過月底自動取月底）
    const ETF_DIST = {
        // 季配息：2/5/8/11月，除息日約18日（依歷史紀錄）
        '00980A': { distFreq: '季配息', distMonths: [2,5,8,11], distDay: 18 },
        // 季配息：3/6/9/12月，除息日約17日（依歷史紀錄）
        '00981A': { distFreq: '季配息', distMonths: [3,6,9,12], distDay: 17 },
        // 季配息：3/6/9/12月（上市2026/05/11，首次配息待確認；暫參考同系列00981A規則）
        '00403A': { distFreq: '季配息', distMonths: [3,6,9,12], distDay: 17 },
        // 季配息：2/5/8/11月，除息日約18日（依歷史紀錄）
        '00982A': { distFreq: '季配息', distMonths: [2,5,8,11], distDay: 18 },
        // 年配息：每年12月底評價（上市2025/07/01，首次配息2026/12）
        '00985A': { distFreq: '年配息', distMonths: [12], distDay: 31 },
        // 年配息：每年10/31後35個營業日內（約12月中旬）（上市2025/12/03）
        '00987A': { distFreq: '年配息', distMonths: [12], distDay: 20 },
        // 年配息：每年9月（上市2025/10，首次配息2026/09）
        '00988A': { distFreq: '年配息', distMonths: [9],  distDay: 30 },
        // 半年配：每年6月底及12月底
        '00991A': { distFreq: '半年配', distMonths: [6,12], distDay: 30 },
        // 季配息：1/4/7/10月（上市2025/12/30，首次配息約2026/04）
        '00992A': { distFreq: '季配息', distMonths: [1,4,7,10], distDay: 25 },
        // 年配息：每年12月底評價（上市2026/02/03，首次配息2026/12）
        '00993A': { distFreq: '年配息', distMonths: [12], distDay: 31 },
        // 季配息：1/4/7/10月（上市2026/01/22，首次評價2026/09）
        '00995A': { distFreq: '季配息', distMonths: [1,4,7,10], distDay: 31 },
    };

    // 計算下一次配息日（distMonths 需已排序）
    const getNextDivDate = (months, day) => {
        const today = new Date(); today.setHours(0,0,0,0);
        for (let yr = today.getFullYear(); yr <= today.getFullYear() + 1; yr++) {
            for (const mo of months) {
                const lastDay = new Date(yr, mo, 0).getDate(); // 當月最後一天
                const candidate = new Date(yr, mo - 1, Math.min(day, lastDay));
                if (candidate >= today) return candidate;
            }
        }
        return null;
    };

    const etfSelector = document.getElementById('etf-selector');

    // ── Snapshots / Manifest ───────────────────────────────────
    const loadManifest = () => {
        if (manifest) return Promise.resolve(manifest);
        if (manifestPromise) return manifestPromise;
        manifestPromise = fetch('snapshots/manifest.json')
            .then(r => r.ok ? r.json() : null)
            .then(m => { manifest = m || { dates: [], etfs: {} }; return manifest; })
            .catch(() => { manifest = { dates: [], etfs: {} }; return manifest; });
        return manifestPromise;
    };

    const refreshDatePicker = (etfId) => {
        const picker = document.getElementById('date-picker');
        if (!picker) return;
        loadManifest().then(m => {
            const etfDates = (m.etfs && m.etfs[etfId]) ? m.etfs[etfId] : [];
            // 由新到舊
            const sorted = [...etfDates].sort().reverse();
            const prevValue = picker.value;
            picker.innerHTML = '<option value="latest">最新</option>'
                + sorted.map(d => `<option value="${d}">${d}</option>`).join('');
            // 保留先前的選擇 (若該 ETF 仍有此日期)
            if (prevValue && (prevValue === 'latest' || sorted.includes(prevValue))) {
                picker.value = prevValue;
            } else {
                picker.value = 'latest';
                currentDate = 'latest';
            }
        });
    };

    const loadSnapshot = (etfId, date) => {
        currentEtfId = etfId;
        currentDate = date;
        sortMode = 'weight';
        resetHeaderIcons();
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:2rem;">載入中，請稍候...</td></tr>';

        fetch(`snapshots/${date}.json`)
            .then(r => { if (!r.ok) throw new Error('no snapshot'); return r.json(); })
            .then(allEtfs => {
                const etfBlock = allEtfs[etfId];
                if (!etfBlock) {
                    showNoDataModal(etfId, date);
                    // revert to latest
                    document.getElementById('date-picker').value = 'latest';
                    loadData(etfId);
                    return;
                }
                renderFromSnapshot(etfId, etfBlock, date);
            })
            .catch(() => {
                showNoDataModal(etfId, date);
                document.getElementById('date-picker').value = 'latest';
                loadData(etfId);
            });
    };

    const showNoDataModal = (etfId, date) => {
        // 重用 history modal 的 DOM
        modalTitle.innerHTML = `<i class="fa-solid fa-circle-exclamation" style="color:#f59e0b;"></i> 該日期無持股資料`;
        modalBody.innerHTML = `
            <div style="text-align:center;padding:2rem 1rem;color:var(--text-secondary);">
                <i class="fa-solid fa-calendar-xmark" style="font-size:2.5rem;opacity:0.4;display:block;margin-bottom:0.75rem;"></i>
                <p style="font-size:1rem;color:var(--text-primary);margin-bottom:0.5rem;">${etfId} 於 ${date} 沒有持股資料</p>
                <p style="font-size:0.82rem;opacity:0.7;">該 ETF 可能尚未發行、或為週末/假日。<br>請改選其他日期。</p>
            </div>`;
        modalOverlay.style.display = 'flex';
    };

    const renderFromSnapshot = (etfId, block, date) => {
        const meta = block.meta || {};
        globalData = block.holdings || [];

        const elSubtitle = document.getElementById('header-subtitle');
        if (elSubtitle) {
            elSubtitle.innerHTML = `<i class="fa-solid fa-clock-rotate-left"></i> 歷史快照模式：<strong style="color:#60a5fa;">${date}</strong> &nbsp; <span style="color:var(--text-secondary);font-size:0.85em;">（股價、市值等顯示當日資料）</span>`;
        }

        const elLastUpdate = document.getElementById('last-update-time');
        if (elLastUpdate) {
            elLastUpdate.innerHTML = `<span style="font-weight:600;color:var(--text-primary);">持股資料日期：${date}</span>　<span style="color:#f59e0b;font-size:0.82em;">（歷史快照）</span>`;
        }

        const elScale = document.getElementById('etf-scale-info');
        if (elScale) {
            const fmtZhang = n => n >= 10000 ? `${(n / 10000).toFixed(1)}萬張` : `${n.toLocaleString()}張`;
            const sharesNow = meta.totalShares || 0;
            const capNow = meta.totalMarketCap || 0;
            elScale.innerHTML = `<i class="fa-solid fa-layer-group"></i> 基金規模：${fmtZhang(sharesNow)} &nbsp;|&nbsp; <i class="fa-solid fa-coins"></i> 市值：${capNow.toFixed(2)}億`;
            elScale.style.display = '';
        }

        // 配息資訊維持顯示
        const elDistFreq = document.getElementById('stat-dist-freq');
        const elDistNext = document.getElementById('stat-dist-next');
        const distCfg = ETF_DIST[etfId];
        if (elDistFreq && distCfg) elDistFreq.textContent = distCfg.distFreq;
        if (elDistNext) elDistNext.textContent = '';

        // 隱藏經理人卡（歷史模式）
        const managerCardImg = document.getElementById('manager-card-img');
        if (managerCardImg) managerCardImg.style.display = 'none';

        applySortAndRender();
    };

    const loadData = (etfId) => {
        currentEtfId = etfId;
        currentDate = 'latest';
        sortMode = 'weight';
        resetHeaderIcons();
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:2rem;">載入中，請稍候...</td></tr>';
        fetch(`data_${etfId}.json?t=${Date.now()}`, { cache: 'no-store' })
            .then(r => { if (!r.ok) throw new Error('無資料檔'); return r.json(); })
            .then(data => {
                const meta = data.meta;
                globalData = data.holdings;
                const elSubtitle = document.getElementById('header-subtitle');
                if (elSubtitle) {
                    const priceDateStr = meta.priceDate
                        ? `<span style="color:#6b7280;font-size:0.82em;margin-left:0.3em;">(${meta.priceDate})</span>`
                        : '';
                    let priceChangeStr = '';
                    if (meta.priceChange != null && meta.priceChange !== 0) {
                        const pc = meta.priceChange;
                        const pcSign = pc >= 0 ? '+' : '';
                        const pcColor = pc >= 0 ? '#ff4d4d' : '#4ade80';
                        let amtStr = '';
                        if (meta.prevPrice > 0) {
                            const amt = Number(meta.etfPrice) - Number(meta.prevPrice);
                            const amtSign = amt >= 0 ? '+' : '';
                            amtStr = `${amtSign}${amt.toFixed(2)}, `;
                        }
                        priceChangeStr = ` <span style="color:${pcColor};font-weight:600;font-size:0.92em;">(${amtStr}${pcSign}${pc.toFixed(2)}%)</span>`;
                    }
                    const priceStr = meta.etfPrice
                        ? ` &nbsp;|&nbsp; <i class="fa-solid fa-dollar-sign"></i> 股價：<span style="color:#60a5fa;font-weight:bold;">${Number(meta.etfPrice).toFixed(2)}</span>${priceChangeStr}${priceDateStr}`
                        : '';
                    elSubtitle.innerHTML = `<i class="fa-solid fa-user-tie"></i> 經理人：${meta.manager}${priceStr} &nbsp;|&nbsp; <i class="fa-solid fa-chart-line"></i> 今年以來(YTD)績效：<span style="color:${meta.ytd >= 0 ? '#ff4d4d' : '#4ade80'};font-weight:bold;">${meta.ytd > 0 ? '+' : ''}${meta.ytd}%</span>`;
                }
                // badge removed
                const elLastUpdate = document.getElementById('last-update-time');
                if (elLastUpdate) {
                    const dataDateStr = meta.dataDate
                        ? `<span style="font-weight:600;color:var(--text-primary);">持股資料日期：${meta.dataDate}</span>　`
                        : '';
                    const lastUpdateStr = meta.lastUpdate ? `最後更新：${meta.lastUpdate}` : '';
                    // Special note for ETFs with inherent 1-day delay (海外 ETF 因美股盤後資料隔日才公布)
                    const GLOBAL_ETFS = ['00988A'];
                    let delayNote = '';
                    if (GLOBAL_ETFS.includes(etfId)) {
                        delayNote = '　<span style="color:#6b7280;font-size:0.78em;">（海外ETF，資料比台灣ETF晚1個交易日）</span>';
                    }
                    elLastUpdate.innerHTML = dataDateStr + lastUpdateStr + delayNote;
                }

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
                        const sharesPct = (sharesDiff / sharesPrev * 100).toFixed(1);
                        const pctSign = sharesDiff > 0 ? '+' : '';
                        sharesDiffStr = ` <span style="color:${color};font-weight:700;">(${arrow}${fmtZhang(Math.abs(sharesDiff))}, ${pctSign}${sharesPct}%)</span>`;
                    }

                    const capNow = meta.totalMarketCap || 0;
                    const capPrev = meta.prevTotalMarketCap || 0;
                    const capDiff = capNow - capPrev;
                    let capDiffStr = '';
                    if (Math.abs(capDiff) >= 0.01 && capPrev > 0) {
                        const arrow = capDiff > 0 ? '↑' : '↓';
                        const color = capDiff > 0 ? '#ff4d4d' : '#4ade80';
                        const capPct = (capDiff / capPrev * 100).toFixed(1);
                        const pctSign = capDiff > 0 ? '+' : '';
                        capDiffStr = ` <span style="color:${color};font-weight:700;">(${arrow}${Math.abs(capDiff).toFixed(2)}億, ${pctSign}${capPct}%)</span>`;
                    }

                    elScale.innerHTML = `<i class="fa-solid fa-layer-group"></i> 基金規模：${fmtZhang(sharesNow)}${sharesDiffStr} &nbsp;|&nbsp; <i class="fa-solid fa-coins"></i> 市值：${capNow.toFixed(2)}億${capDiffStr}`;
                    elScale.style.display = '';
                } else if (elScale) {
                    elScale.style.display = 'none';
                }
                // 00981A 專屬經理人卡片圖
                const managerCardImg = document.getElementById('manager-card-img');
                if (managerCardImg) {
                    managerCardImg.style.display = etfId === '00981A' ? 'inline-block' : 'none';
                    // 點擊放大 lightbox（只綁定一次）
                    if (!managerCardImg._lightboxBound) {
                        managerCardImg._lightboxBound = true;
                        managerCardImg.addEventListener('click', () => {
                            const overlay = document.getElementById('lightbox-overlay');
                            if (overlay) overlay.style.display = 'flex';
                        });
                    }
                }
                // ESC 關閉 lightbox（只綁定一次）
                if (!window._lightboxEscBound) {
                    window._lightboxEscBound = true;
                    document.addEventListener('keydown', e => {
                        if (e.key === 'Escape') {
                            const overlay = document.getElementById('lightbox-overlay');
                            if (overlay) overlay.style.display = 'none';
                        }
                    });
                }

                // 配息資訊
                const elDistFreq = document.getElementById('stat-dist-freq');
                const elDistNext = document.getElementById('stat-dist-next');
                const distCfg = ETF_DIST[etfId];
                if (elDistFreq && distCfg) {
                    elDistFreq.textContent = distCfg.distFreq;
                    if (elDistNext) {
                        const nextDate = getNextDivDate(distCfg.distMonths, distCfg.distDay);
                        if (nextDate) {
                            const y = nextDate.getFullYear();
                            const m = nextDate.getMonth() + 1;
                            const d = nextDate.getDate();
                            elDistNext.textContent = `下次配息日 ${y}/${m}/${d}`;
                        } else {
                            elDistNext.textContent = '';
                        }
                    }
                }

                applySortAndRender();
            })
            .catch(err => {
                console.error(err);
                tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;color:#ef4444;padding:2rem;">無法載入 ${etfId} 的持股資料。</td></tr>`;
            });
    };

    etfSelector.addEventListener('change', e => {
        const id = e.target.value;
        loadData(id);
        refreshDatePicker(id);
        document.querySelectorAll('.ytd-item').forEach(el => {
            el.classList.toggle('ytd-item-active', el.dataset.etf === id);
        });
    });

    const datePicker = document.getElementById('date-picker');
    if (datePicker) {
        datePicker.addEventListener('change', e => {
            const v = e.target.value;
            if (v === 'latest') {
                loadData(currentEtfId);
            } else {
                loadSnapshot(currentEtfId, v);
            }
        });
    }

    loadData(etfSelector.value);
    refreshDatePicker(etfSelector.value);

    // ── YTD Ranking ────────────────────────────────────────────
    const ALL_ETFS = [
        { id: '00981A', name: '統一台股增長' },
        { id: '00403A', name: '統一升級50' },
        { id: '00988A', name: '統一全球創新' },
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

        const tBust = Date.now();
        Promise.all(ALL_ETFS.map(etf =>
            fetch(`data_${etf.id}.json?t=${tBust}`, { cache: 'no-store' })
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
        { id: '00403A', name: '統一升級50' },
        { id: '00988A', name: '統一全球創新' },
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

            const sortedEtfs = [...row.etfs].sort((a, b) => b.weight - a.weight);
            const etfTags = sortedEtfs.map(e => `
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

    const loadCrossData = (force = false) => {
        if (crossLoaded && !force) return;
        const crossBody = document.getElementById('cross-body');
        crossBody.innerHTML = '<tr><td colspan="4" style="text-align:center;padding:2rem;">載入中，請稍候...</td></tr>';

        const bust = `?t=${Date.now()}`;
        Promise.all(ETF_LIST.map(etf =>
            fetch(`data_${etf.id}.json${bust}`, { cache: 'no-store' })
                .then(r => r.ok ? r.json() : null)
                .then(data => data ? { etf, holdings: data.holdings, meta: data.meta } : null)
                .catch(() => null)   // 單一 ETF 失敗不能拖累整個 Promise.all
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
                        yestWeight: h.yestWeight,
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

            const weightDisplay = (() => {
                const prev = etf.yestWeight, curr = etf.weight;
                if (!curr && curr !== 0) return '-';
                if (prev == null || prev === undefined) return `<span class="weight-pill">${curr}%</span>`;
                if (prev === curr) return `<span class="weight-pill">${curr}%</span>`;
                const diff = curr - prev;
                const color = curr > prev ? '#ff4d4d' : '#4ade80';
                return `<span style="color:#9ca3af;font-size:0.8em;">${prev}%</span> <span style="color:${color};">→</span> <span class="weight-pill">${curr}%</span> <span style="color:${color};font-size:0.8em;">(${diff > 0 ? '+' : ''}${diff.toFixed(2)}%)</span>`;
            })();

            tr.innerHTML = `
                <td data-label="序號"><span style="display:inline-block;width:30px;height:30px;line-height:30px;text-align:center;border-radius:50%;background:#334155;color:#fff;font-weight:bold;">${index + 1}</span></td>
                <td data-label="持有 ETF"><div class="stock-id">${etf.etfId}</div><div class="stock-name">${etf.etfName}</div></td>
                <td data-label="權重佔比" class="align-right">${weightDisplay}</td>
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

    // ── Common Add/Reduce Tab ──────────────────────────────────
    let commonLoaded = false;
    let commonCurrentDate = 'latest';

    const classifyAction = (curr, prev) => {
        // returns 'add' (新增/加碼), 'reduce' (減碼/出清), or null
        prev = prev ?? 0;
        if (curr > prev) return 'add';        // 加碼 (含新增 prev=0)
        if (curr < prev) return 'reduce';     // 減碼 (含出清 curr=0)
        return null;
    };

    const renderCommonRows = (rows, tbodyId, color) => {
        const body = document.getElementById(tbodyId);
        body.innerHTML = '';
        if (rows.length === 0) {
            body.innerHTML = `<tr><td colspan="4" style="text-align:center;color:var(--text-secondary);padding:1.5rem;">今日無 2 檔以上 ETF 共同${tbodyId.includes('add') ? '加碼' : '減碼'}的標的</td></tr>`;
            return;
        }
        rows.forEach((row, index) => {
            const tr = document.createElement('tr');
            tr.style.animation = `fadeInUp 0.3s cubic-bezier(0.16,1,0.3,1) ${Math.min(0.05 + index * 0.015, 0.8)}s forwards`;
            tr.style.opacity = '0';
            tr.style.transform = 'translateY(10px)';
            const tags = row.etfs.map(e => {
                const prev = e.yestWeight, curr = e.todayWeight;
                let weightHtml = '';
                if (curr != null) {
                    if (prev == null || prev === 0) {
                        // 新增
                        weightHtml = `<span class="etf-tag-weight" style="background:rgba(167,139,250,0.2);color:#a78bfa;">+${curr}%</span>`;
                    } else if (prev === curr) {
                        weightHtml = `<span class="etf-tag-weight">${curr}%</span>`;
                    } else {
                        const color = curr > prev ? '#ff4d4d' : '#4ade80';
                        weightHtml = `<span style="color:#9ca3af;font-size:0.78em;">${prev}%</span> <span style="color:${color};font-weight:700;">→</span> <span class="etf-tag-weight" style="color:${color};background:${curr > prev ? 'rgba(239,68,68,0.15)' : 'rgba(74,222,128,0.15)'};">${curr}%</span>`;
                    }
                } else if (prev != null && prev > 0) {
                    // 出清
                    weightHtml = `<span style="color:#9ca3af;font-size:0.78em;">${prev}%</span> <span style="color:#f97316;font-weight:700;">→</span> <span class="etf-tag-weight" style="color:#f97316;background:rgba(249,115,22,0.15);">0%</span>`;
                }
                return `
                <span class="etf-tag">
                    <span class="etf-tag-id">${e.etfId}</span>
                    <span class="etf-tag-name">${e.etfName}</span>
                    ${weightHtml}
                </span>`;
            }).join('');
            tr.innerHTML = `
                <td data-label="序號"><span style="display:inline-block;width:30px;height:30px;line-height:30px;text-align:center;border-radius:50%;background:#334155;color:#fff;font-weight:bold;">${index + 1}</span></td>
                <td data-label="股票"><div class="stock-id">${row.code}</div><div class="stock-name">${row.name}</div></td>
                <td data-label="ETF 清單"><div class="etf-tags">${tags}</div></td>
                <td data-label="ETF 數" class="align-right"><span class="cross-count-badge" style="background:${color};color:#fff;">${row.etfs.length}</span></td>
            `;
            body.appendChild(tr);
        });
    };

    const renderAmountRanking = (rows) => {
        const body = document.getElementById('common-amount-body');
        if (!body) return;
        body.innerHTML = '';
        if (rows.length === 0) {
            body.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-secondary);padding:1.5rem;">今日無加減碼金額資料</td></tr>';
            return;
        }
        const fmt = n => Number(Math.abs(Math.round(n))).toLocaleString('zh-TW');
        rows.forEach((r, index) => {
            const tr = document.createElement('tr');
            tr.style.animation = `fadeInUp 0.3s cubic-bezier(0.16,1,0.3,1) ${Math.min(0.05 + index * 0.012, 0.8)}s forwards`;
            tr.style.opacity = '0';
            tr.style.transform = 'translateY(10px)';

            const addStr = r.addAmount > 0
                ? `<span style="color:#ff4d4d;font-weight:700;">+$${fmt(r.addAmount)}</span>`
                : `<span style="color:#6b7280;">—</span>`;
            const reduceStr = r.reduceAmount < 0
                ? `<span style="color:#4ade80;font-weight:700;">-$${fmt(r.reduceAmount)}</span>`
                : `<span style="color:#6b7280;">—</span>`;
            const shColor  = r.totalShares > 0 ? '#ff4d4d' : '#4ade80';
            const shSign   = r.totalShares > 0 ? '+' : '-';

            // 序號排名背景：前三名做漸層
            const rankStyles = [
                'background:linear-gradient(135deg,#f59e0b,#d97706);color:#fff;box-shadow:0 2px 8px rgba(245,158,11,0.5);',
                'background:linear-gradient(135deg,#94a3b8,#64748b);color:#fff;',
                'background:linear-gradient(135deg,#cd7c2f,#a16207);color:#fff;',
            ];
            const rankStyle = rankStyles[index] || 'background:#334155;color:#fff;';

            tr.innerHTML = `
                <td data-label="序號"><span style="display:inline-block;width:30px;height:30px;line-height:30px;text-align:center;border-radius:50%;font-weight:bold;${rankStyle}">${index + 1}</span></td>
                <td data-label="股票"><div class="stock-id">${r.code}</div><div class="stock-name">${r.name}</div></td>
                <td data-label="加碼金額" class="align-right">${addStr}</td>
                <td data-label="減碼金額" class="align-right">${reduceStr}</td>
                <td data-label="累計股數" class="align-right"><span style="color:${shColor};font-weight:600;">${shSign}${fmt(r.totalShares)}</span></td>
                <td data-label="ETF 數" class="align-right">
                    <span class="amount-etf-badge" data-stock-code="${r.code}" style="display:inline-block;cursor:pointer;background:rgba(250,204,21,0.18);border:1px solid rgba(250,204,21,0.4);color:#facc15;width:2rem;height:2rem;line-height:1.9rem;text-align:center;border-radius:50%;font-weight:700;transition:transform 0.15s, background 0.15s;" title="點擊查看 ETF 清單">${r.etfs.length}</span>
                </td>
            `;
            // 點擊 badge → 開 modal 顯示 ETF 名稱與各自加減碼明細
            tr.querySelector('.amount-etf-badge').addEventListener('click', e => {
                e.stopPropagation();
                openAmountEtfsModal(r);
            });
            body.appendChild(tr);
        });
    };

    const refreshCommonDatePicker = () => {
        const picker = document.getElementById('common-date-picker');
        if (!picker) return;
        loadManifest().then(m => {
            const dates = (m.dates || []).slice().sort().reverse();
            const prev = picker.value;
            picker.innerHTML = '<option value="latest">最新</option>'
                + dates.map(d => `<option value="${d}">${d}</option>`).join('');
            if (prev && (prev === 'latest' || dates.includes(prev))) picker.value = prev;
        });
    };

    const loadCommonActions = (force = false) => {
        if (commonLoaded && !force) return;
        document.getElementById('common-add-body').innerHTML =
            '<tr><td colspan="4" style="text-align:center;padding:2rem;">載入中...</td></tr>';
        document.getElementById('common-reduce-body').innerHTML =
            '<tr><td colspan="4" style="text-align:center;padding:2rem;">載入中...</td></tr>';

        const bust = `?t=${Date.now()}`;   // bypass GitHub Pages / browser cache
        const fetchPromise = commonCurrentDate === 'latest'
            ? Promise.all(ETF_LIST.map(etf =>
                fetch(`data_${etf.id}.json${bust}`, { cache: 'no-store' })
                    .then(r => r.ok ? r.json() : null)
                    .then(data => data ? { etf, holdings: data.holdings, meta: data.meta } : null)
                    .catch(() => null)
              ))
            : fetch(`snapshots/${commonCurrentDate}.json`)
                .then(r => r.ok ? r.json() : null)
                .then(snap => {
                    if (!snap) return [];
                    return ETF_LIST.map(etf => {
                        const block = snap[etf.id];
                        return block ? { etf, holdings: block.holdings, meta: block.meta } : null;
                    });
                })
                .catch(() => []);

        Promise.resolve(fetchPromise).then(results => {
            const valid = results.filter(Boolean);
            const addMap = new Map();    // code -> {code, name, etfs[]}
            const reduceMap = new Map();
            const amountMap = new Map(); // code -> {code, name, totalAmount, totalShares, etfCount}

            valid.forEach(({ etf, holdings }) => {
                holdings.forEach(h => {
                    const action = classifyAction(h.shares, h.prevShares);
                    if (!action) return;
                    const target = action === 'add' ? addMap : reduceMap;
                    if (!target.has(h.code)) {
                        target.set(h.code, { code: h.code, name: h.name, etfs: [] });
                    }
                    target.get(h.code).etfs.push({
                        etfId: etf.id,
                        etfName: etf.name,
                        yestWeight: h.yestWeight,
                        todayWeight: h.todayWeight,
                    });

                    // 累計金額排行：所有有變動的 ETF 都列入計算 (含單一 ETF)
                    if (!amountMap.has(h.code)) {
                        amountMap.set(h.code, { code: h.code, name: h.name, addAmount: 0, reduceAmount: 0, totalAmount: 0, totalShares: 0, etfs: [] });
                    }
                    const m = amountMap.get(h.code);
                    const da = h.diffAmount || 0;
                    if (da > 0) m.addAmount += da;       // 加碼金額（正值加總）
                    else        m.reduceAmount += da;    // 減碼金額（負值加總）
                    m.totalAmount += da;
                    m.totalShares += (h.diffShares || 0);
                    m.etfs.push({
                        etfId: etf.id,
                        etfName: etf.name,
                        diffShares: h.diffShares || 0,
                        diffAmount: da,
                    });
                });
            });

            const addRows = Array.from(addMap.values())
                .filter(s => s.etfs.length >= 2)
                .sort((a, b) => b.etfs.length - a.etfs.length || a.code.localeCompare(b.code));
            const reduceRows = Array.from(reduceMap.values())
                .filter(s => s.etfs.length >= 2)
                .sort((a, b) => b.etfs.length - a.etfs.length || a.code.localeCompare(b.code));

            renderCommonRows(addRows, 'common-add-body', 'linear-gradient(135deg,#ef4444,#b91c1c)');
            renderCommonRows(reduceRows, 'common-reduce-body', 'linear-gradient(135deg,#22c55e,#15803d)');

            // 累計金額排行（依加碼+減碼絕對值總額由大到小）
            const amountRows = Array.from(amountMap.values())
                .filter(r => r.addAmount > 0 || r.reduceAmount < 0)
                .sort((a, b) => (b.addAmount + Math.abs(b.reduceAmount)) - (a.addAmount + Math.abs(a.reduceAmount)));
            renderAmountRanking(amountRows);

            const badge = document.getElementById('common-badge');
            if (badge) badge.textContent = `加碼 ${addRows.length} 檔 / 減碼 ${reduceRows.length} 檔`;
            const elUpd = document.getElementById('common-update-time');
            if (elUpd) {
                if (commonCurrentDate === 'latest') {
                    const dates = valid.map(v => v.meta && v.meta.lastUpdate).filter(Boolean).sort();
                    elUpd.textContent = dates.length ? `資料更新時間：${dates[dates.length - 1]}` : '';
                } else {
                    elUpd.innerHTML = `<span style="font-weight:600;color:var(--text-primary);">資料日期：${commonCurrentDate}</span>　<span style="color:#f59e0b;font-size:0.82em;">（歷史快照）</span>`;
                }
            }

            commonLoaded = true;
        });
    };

    const commonDatePicker = document.getElementById('common-date-picker');
    if (commonDatePicker) {
        commonDatePicker.addEventListener('change', e => {
            commonCurrentDate = e.target.value;
            loadCommonActions(true);
        });
    }
    refreshCommonDatePicker();

    // ── Stock History Modal ────────────────────────────────────
    // 動態建立 modal 並 inline 所有關鍵樣式，避免依賴 CSS / HTML 快取狀態
    const ensureModal = () => {
        // 移除舊版（若存在）以保證 inline style 一致
        const existing = document.getElementById('history-modal-overlay');
        if (existing) existing.remove();

        const overlay = document.createElement('div');
        overlay.id = 'history-modal-overlay';
        overlay.setAttribute('style', [
            'position:fixed',
            'top:0', 'left:0', 'right:0', 'bottom:0',
            'width:100vw', 'height:100vh',
            'z-index:2147483647',
            'background:rgba(0,0,0,0.75)',
            'backdrop-filter:blur(6px)',
            '-webkit-backdrop-filter:blur(6px)',
            'display:none',
            'align-items:center',
            'justify-content:center',
            'padding:1rem',
            'opacity:1',
            'visibility:visible',
        ].join(';'));

        overlay.innerHTML = `
            <div id="history-modal-box" style="
                width:100%;max-width:560px;max-height:85vh;
                display:flex;flex-direction:column;padding:1.5rem;
                background:rgba(30,41,59,0.95);
                backdrop-filter:blur(16px);
                border:1px solid rgba(255,255,255,0.08);
                border-radius:1.5rem;
                box-shadow:0 20px 60px rgba(0,0,0,0.8);
                opacity:1;
            " onclick="event.stopPropagation()">
                <div style="display:flex;align-items:center;justify-content:space-between;padding-bottom:0.75rem;border-bottom:1px solid rgba(255,255,255,0.08);margin-bottom:1rem;">
                    <h3 id="history-modal-title" style="font-size:1.05rem;font-weight:600;color:#f8fafc;margin:0;"><i class="fa-solid fa-clock-rotate-left"></i> 加減碼紀錄</h3>
                    <button id="history-modal-close" style="
                        width:32px;height:32px;border-radius:50%;
                        background:rgba(255,255,255,0.05);
                        border:1px solid rgba(255,255,255,0.08);
                        color:#94a3b8;cursor:pointer;
                        display:flex;align-items:center;justify-content:center;
                        font-size:1rem;
                    "><i class="fa-solid fa-xmark"></i></button>
                </div>
                <div id="history-modal-body" style="flex:1;overflow-y:auto;color:#f8fafc;">
                    <p style="color:#94a3b8;text-align:center;padding:1rem;">載入中...</p>
                </div>
            </div>`;
        document.body.appendChild(overlay);
        return overlay;
    };
    const modalOverlay = ensureModal();
    const modalTitle = document.getElementById('history-modal-title');
    const modalBody = document.getElementById('history-modal-body');
    const modalClose = document.getElementById('history-modal-close');
    console.log('[ETF Tracker] modal ready:', !!modalOverlay, !!modalTitle, !!modalBody);

    const loadHistory = () => {
        if (historyData) return Promise.resolve(historyData);
        if (historyPromise) return historyPromise;
        historyPromise = fetch('history.json')
            .then(r => r.ok ? r.json() : {})
            .then(d => { historyData = d; return d; })
            .catch(() => { historyData = {}; return {}; });
        return historyPromise;
    };

    const closeModal = () => {
        modalOverlay.style.display = 'none';
    };
    if (modalClose) modalClose.addEventListener('click', closeModal);
    if (modalOverlay) modalOverlay.addEventListener('click', e => {
        if (e.target === modalOverlay) closeModal();
    });
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && modalOverlay.style.display !== 'none') closeModal();
    });

    const fmt = n => Number(Math.abs(n)).toLocaleString('zh-TW');

    const openHistoryModal = (code, name) => {
        console.log('[ETF Tracker] openHistoryModal called:', code, name);
        try {
            modalTitle.innerHTML = `<i class="fa-solid fa-clock-rotate-left"></i> ${code} ${name} <span style="color:var(--text-secondary);font-size:0.82em;font-weight:400;margin-left:0.4em;">@ ${currentEtfId}</span>`;
            modalBody.innerHTML = '<p style="color:var(--text-secondary);text-align:center;padding:1rem;"><i class="fa-solid fa-spinner fa-spin"></i> 載入中...</p>';
            modalOverlay.style.display = 'flex';
            const cs = getComputedStyle(modalOverlay);
            console.log('[ETF Tracker] modal displayed, display=', cs.display, 'visibility=', cs.visibility, 'opacity=', cs.opacity, 'z-index=', cs.zIndex);
        } catch (err) {
            console.error('[ETF Tracker] modal open error:', err);
            return;
        }

        loadHistory().then(data => {
            const records = data?.[currentEtfId]?.[code] || [];
            if (!records.length) {
                modalBody.innerHTML = `
                    <div style="text-align:center;padding:2rem 1rem;color:var(--text-secondary);">
                        <i class="fa-solid fa-circle-info" style="font-size:2rem;opacity:0.4;display:block;margin-bottom:0.75rem;"></i>
                        近期無加減碼紀錄
                        <p style="font-size:0.78rem;margin-top:0.5rem;opacity:0.6;">（${currentEtfId} 對 ${code} ${name} 尚未出現股數變動）</p>
                    </div>`;
                return;
            }

            const rowsHtml = records.map(([date, ds, da]) => {
                const dsColor = ds > 0 ? '#ff4d4d' : '#4ade80';
                const dsSign = ds > 0 ? '+' : '-';
                const daColor = da > 0 ? '#ff4d4d' : '#4ade80';
                const daSign = da > 0 ? '+' : '-';
                const amtStr = da !== 0
                    ? `<span style="color:${daColor};font-weight:600;">${daSign}$${fmt(Math.round(da))}</span>`
                    : `<span style="color:#6b7280;">—</span>`;
                return `
                    <tr>
                        <td>${date}</td>
                        <td class="align-right"><span style="color:${dsColor};font-weight:700;">${dsSign}${fmt(ds)}</span></td>
                        <td class="align-right">${amtStr}</td>
                    </tr>`;
            }).join('');

            const totalShares = records.reduce((s, r) => s + r[1], 0);
            const totalAmount = records.reduce((s, r) => s + r[2], 0);
            const totalSharesColor = totalShares >= 0 ? '#ff4d4d' : '#4ade80';
            const totalSharesSign = totalShares >= 0 ? '+' : '-';
            const totalAmtColor = totalAmount >= 0 ? '#ff4d4d' : '#4ade80';
            const totalAmtSign = totalAmount >= 0 ? '+' : '-';

            modalBody.innerHTML = `
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.75rem;padding:0.75rem 1rem;background:rgba(255,255,255,0.03);border-radius:0.5rem;font-size:0.85rem;">
                    <span style="color:var(--text-secondary);">共 <strong style="color:var(--text-primary);">${records.length}</strong> 筆變動</span>
                    <span>累計 <span style="color:${totalSharesColor};font-weight:700;">${totalSharesSign}${fmt(totalShares)}</span> 股　/　<span style="color:${totalAmtColor};font-weight:700;">${totalAmtSign}$${fmt(Math.round(Math.abs(totalAmount)))}</span></span>
                </div>
                <table class="history-table">
                    <thead>
                        <tr>
                            <th>日期</th>
                            <th class="align-right">加減碼股數</th>
                            <th class="align-right">加減碼金額</th>
                        </tr>
                    </thead>
                    <tbody>${rowsHtml}</tbody>
                </table>`;
        });
    };

    // 點擊累計金額排行的 ETF 數 badge 開啟 modal，列出該股有變動的所有 ETF
    const openAmountEtfsModal = (row) => {
        modalTitle.innerHTML = `<i class="fa-solid fa-coins" style="color:#facc15;"></i> ${row.code} ${row.name} <span style="color:var(--text-secondary);font-size:0.82em;font-weight:400;margin-left:0.4em;">變動 ETF 清單</span>`;

        // 依加減碼金額簽號值由大到小排序：買最多放上面、賣最多放下面
        const sorted = [...row.etfs].sort((a, b) => b.diffAmount - a.diffAmount);

        const totalAmtColor = row.totalAmount >= 0 ? '#ff4d4d' : '#4ade80';
        const totalAmtSign = row.totalAmount >= 0 ? '+' : '-';
        const totalShColor = row.totalShares >= 0 ? '#ff4d4d' : '#4ade80';
        const totalShSign = row.totalShares >= 0 ? '+' : '-';

        const rowsHtml = sorted.map(e => {
            const amtColor = e.diffAmount > 0 ? '#ff4d4d' : (e.diffAmount < 0 ? '#4ade80' : '#6b7280');
            const amtSign = e.diffAmount > 0 ? '+' : (e.diffAmount < 0 ? '-' : '');
            const shColor = e.diffShares > 0 ? '#ff4d4d' : (e.diffShares < 0 ? '#4ade80' : '#6b7280');
            const shSign = e.diffShares > 0 ? '+' : (e.diffShares < 0 ? '-' : '');
            return `
                <tr>
                    <td><div style="color:#60a5fa;font-weight:700;">${e.etfId}</div><div style="color:var(--text-secondary);font-size:0.82em;">${e.etfName}</div></td>
                    <td class="align-right"><span style="color:${shColor};font-weight:700;">${shSign}${fmt(e.diffShares)}</span></td>
                    <td class="align-right"><span style="color:${amtColor};font-weight:700;">${amtSign}$${fmt(Math.round(Math.abs(e.diffAmount)))}</span></td>
                </tr>`;
        }).join('');

        modalBody.innerHTML = `
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.75rem;padding:0.75rem 1rem;background:rgba(255,255,255,0.03);border-radius:0.5rem;font-size:0.85rem;">
                <span style="color:var(--text-secondary);">共 <strong style="color:var(--text-primary);">${row.etfs.length}</strong> 檔 ETF 變動</span>
                <span>累計 <span style="color:${totalShColor};font-weight:700;">${totalShSign}${fmt(row.totalShares)}</span> 股　/　<span style="color:${totalAmtColor};font-weight:700;">${totalAmtSign}$${fmt(Math.round(Math.abs(row.totalAmount)))}</span></span>
            </div>
            <table class="history-table">
                <thead>
                    <tr>
                        <th>ETF</th>
                        <th class="align-right">加減碼股數</th>
                        <th class="align-right">加減碼金額</th>
                    </tr>
                </thead>
                <tbody>${rowsHtml}</tbody>
            </table>`;

        modalOverlay.style.display = 'flex';
    };

});
