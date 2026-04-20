// app.js

document.addEventListener('DOMContentLoaded', () => {
    const tbody = document.getElementById('holdings-body');
    const updateBadge = document.getElementById('update-date');
    const thDiffAmount = document.getElementById('th-diff-amount');
    
    // Sort states: 0 = default (todayWeight desc), 1 = diffAmount desc, -1 = diffAmount asc
    let diffSortState = 0;
    let globalData = [];
    
    // Initial badge state
    updateBadge.textContent = `最新交易日差異比較 (...)`;
    
    // Number formatter
    const formatNumber = (num, decimals=0) => {
        return Number(Math.abs(num)).toLocaleString('zh-TW', {
            minimumFractionDigits: decimals,
            maximumFractionDigits: decimals
        });
    };

    const renderDiff = (num, decimals=0) => {
        const absStr = formatNumber(num, decimals);
        if (num > 0) {
            return `<span style="color: #ff4d4d; font-weight: bold;">+${absStr}</span>`;
        } else if (num < 0) {
            return `<span style="color: #4ade80; font-weight: bold;">-${absStr}</span>`;
        } else {
            return `<span style="color: #6b7280;">0</span>`;
        }
    };

    const renderStatus = (holding) => {
        const prev = holding.prevShares ?? null;
        const curr = holding.shares;
        let label = '';
        let style = '';
        if (prev === null || prev === undefined) {
            // No previous data, can't determine
            label = '-';
            style = 'color: #6b7280;';
        } else if (prev === 0 && curr > 0) {
            label = '新增';
            style = 'color: #a78bfa; font-weight: bold;';
        } else if (prev > 0 && curr === 0) {
            label = '出清';
            style = 'color: #f97316; font-weight: bold;';
        } else if (curr > prev) {
            label = '加碼';
            style = 'color: #ff4d4d; font-weight: bold;';
        } else if (curr < prev) {
            label = '減碼';
            style = 'color: #4ade80; font-weight: bold;';
        } else {
            label = '-';
            style = 'color: #6b7280;';
        }
        return `<span style="${style}">${label}</span>`;
    };

    const renderTable = (holdings) => {
        tbody.innerHTML = '';

        holdings.forEach((holding, index) => {
            const tr = document.createElement('tr');

            // Animation stagger
            tr.style.animation = `fadeInUp 0.3s cubic-bezier(0.16, 1, 0.3, 1) ${Math.min(0.1 + (index * 0.02), 1)}s forwards`;
            tr.style.opacity = '0';
            tr.style.transform = 'translateY(10px)';

            tr.innerHTML = `
                <td>
                    <span style="display:inline-block; width: 30px; height: 30px; line-height: 30px; text-align: center; border-radius: 50%; background: #334155; color: #fff; font-weight: bold;">
                        ${holding.rank}
                    </span>
                </td>
                <td class="stock-id">${holding.code}</td>
                <td class="stock-name">${holding.name}</td>
                <td class="stock-shares">${formatNumber(holding.shares)}</td>
                <td class="align-right stock-price">
                    $${formatNumber(holding.price, 2)}
                </td>
                <td class="align-right">
                    <span class="weight-pill" style="opacity: 0.7;">${holding.yestWeight ? holding.yestWeight + '%' : '-'}</span>
                </td>
                <td class="align-right">
                    <span class="weight-pill">${holding.todayWeight ? holding.todayWeight + '%' : '-'}</span>
                </td>
                <td class="align-right">
                    ${renderStatus(holding)}
                </td>
                <td class="align-right">
                    ${renderDiff(holding.diffShares, 0)}
                </td>
                <td class="align-right">
                    $${renderDiff(holding.diffAmount, 0)}
                </td>
            `;

            tbody.appendChild(tr);
        });
    };

    const applySortAndRender = () => {
        let sortedData = [...globalData];
        if (diffSortState === 1) {
            // Sort by diffAmount descending (most positive first)
            sortedData.sort((a, b) => b.diffAmount - a.diffAmount);
            thDiffAmount.innerHTML = '<i class="fa-solid fa-sack-dollar"></i> 加/減碼金額 <i class="fa-solid fa-sort-down"></i>';
        } else if (diffSortState === -1) {
            // Sort by diffAmount ascending (most negative first)
            sortedData.sort((a, b) => a.diffAmount - b.diffAmount);
            thDiffAmount.innerHTML = '<i class="fa-solid fa-sack-dollar"></i> 加/減碼金額 <i class="fa-solid fa-sort-up"></i>';
        } else {
            // Default sort by todayWeight descending
            sortedData.sort((a, b) => b.todayWeight - a.todayWeight);
            thDiffAmount.innerHTML = '<i class="fa-solid fa-sack-dollar"></i> 加/減碼金額 <i class="fa-solid fa-sort" style="opacity:0.3"></i>';
        }
        renderTable(sortedData);
    };

    thDiffAmount.addEventListener('click', () => {
        diffSortState = diffSortState === 0 ? 1 : diffSortState === 1 ? -1 : 0;
        applySortAndRender();
    });

    const etfSelector = document.getElementById('etf-selector');

    const loadData = (etfId) => {
        tbody.innerHTML = '<tr><td colspan="10" style="text-align:center; padding: 2rem;">載入中，請稍候...</td></tr>';
        
        fetch(`data_${etfId}.json`)
            .then(response => {
                if(!response.ok) throw new Error("無資料檔");
                return response.json();
            })
            .then(data => {
                const meta = data.meta;
                globalData = data.holdings;
                
                // Render Subtitle & Date
                const elSubtitle = document.getElementById('header-subtitle');
                if (elSubtitle) {
                    const priceStr = meta.etfPrice ? ` &nbsp;|&nbsp; <i class="fa-solid fa-dollar-sign"></i> 股價：<span style="color: #60a5fa; font-weight: bold;">${Number(meta.etfPrice).toFixed(2)}</span>` : '';
                    elSubtitle.innerHTML = `<i class="fa-solid fa-user-tie"></i> 經理人：${meta.manager}${priceStr} &nbsp;|&nbsp; <i class="fa-solid fa-chart-line"></i> 今年以來(YTD)績效：<span style="color: ${meta.ytd >= 0 ? '#ff4d4d' : '#4ade80'}; font-weight: bold;">${meta.ytd > 0 ? '+' : ''}${meta.ytd}%</span>`;
                }
                
                if (meta.dataDate) {
                    updateBadge.textContent = `最新交易日差異比較 (${meta.dataDate})`;
                }

                const elLastUpdate = document.getElementById('last-update-time');
                if (elLastUpdate && meta.lastUpdate) {
                    elLastUpdate.textContent = `最後更新時間：${meta.lastUpdate}`;
                }

                applySortAndRender();
            })
            .catch(error => {
                console.error("Error fetching data:", error);
                tbody.innerHTML = `<tr><td colspan="10" style="text-align:center; color: #ef4444; padding: 2rem;">無法載入 ${etfId} 的持股資料。</td></tr>`;
            });
    };

    etfSelector.addEventListener('change', (e) => {
        loadData(e.target.value);
    });

    // Initial load
    loadData(etfSelector.value);
});
