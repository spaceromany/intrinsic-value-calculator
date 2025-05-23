// 관심종목 관리 클래스
class WatchlistManager {
    constructor() {
        this.watchlist = this.loadWatchlist();
    }

    // localStorage에서 관심종목 로드
    loadWatchlist() {
        const saved = localStorage.getItem('watchlist');
        return saved ? JSON.parse(saved) : [];
    }

    // 관심종목 저장
    saveWatchlist() {
        localStorage.setItem('watchlist', JSON.stringify(this.watchlist));
    }

    // 관심종목 추가
    addToWatchlist(code) {
        if (!this.watchlist.some(item => item.code === code)) {
            this.watchlist.push({ code });
            this.saveWatchlist();
        }
    }

    // 관심종목 제거
    removeFromWatchlist(code) {
        this.watchlist = this.watchlist.filter(item => item.code !== code);
        this.saveWatchlist();
    }

    // 관심종목 목록 가져오기
    getWatchlist() {
        return this.watchlist;
    }

    // 수익률 계산
    calculateReturn(stock) {
        if (!stock.purchase_price || !stock.current_price) return null;
        return Math.round((stock.current_price - stock.purchase_price) / stock.purchase_price * 100);
    }

    calculateTotalValue(stock) {
        if (!stock.purchase_price || !stock.current_price || !stock.purchase_quantity) return null;
        return Math.round(stock.current_price * stock.purchase_quantity);
    }

    calculateTotalReturn(stock) {
        if (!stock.purchase_price || !stock.current_price || !stock.purchase_quantity) return null;
        const totalPurchaseValue = stock.purchase_price * stock.purchase_quantity;
        const totalCurrentValue = stock.current_price * stock.purchase_quantity;
        return Math.round((totalCurrentValue - totalPurchaseValue) / totalPurchaseValue * 100);
    }

    // 엑셀 내보내기
    async exportToExcel(stocks, limit, dividendFilter) {
        try {
            const response = await fetch('/watchlist/export', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    stocks: stocks,
                    dividend_filter: dividendFilter,
                    limit: limit
                })
            });

            if (!response.ok) {
                throw new Error('엑셀 다운로드 중 오류가 발생했습니다.');
            }

            const blob = await response.blob();
            const downloadUrl = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = downloadUrl;
            a.download = `안전마진_상위${limit}종목${dividendFilter ? '_배당수익률' + dividendFilter + '%이상' : ''}.xlsx`;
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(downloadUrl);
            document.body.removeChild(a);
        } catch (error) {
            console.error('Export error:', error);
            alert('엑셀 내보내기 중 오류가 발생했습니다.');
        }
    }

    updateWatchlistUI() {
        const watchlistContainer = document.getElementById('watchlistGrid');
        if (!watchlistContainer) return;

        if (this.watchlist.length === 0) {
            watchlistContainer.innerHTML = '<div class="text-center text-muted">관심종목이 없습니다.</div>';
            return;
        }

        const html = this.watchlist.map(stock => {
            const returnRate = this.calculateReturn(stock);
            const totalValue = this.calculateTotalValue(stock);
            const totalReturn = this.calculateTotalReturn(stock);
            const returnClass = returnRate > 0 ? 'text-success' : returnRate < 0 ? 'text-danger' : '';
            
            return `
                <div class="col-12 mb-2">
                    <div class="card">
                        <div class="card-body p-3">
                            <div class="d-flex align-items-center mb-2">
                                <div class="heart-icon me-3" style="cursor: pointer;" onclick="event.stopPropagation(); window.watchlistManager.removeFromWatchlist('${stock.code}')">
                                    <i class="fas fa-heart" style="font-size: 1.2rem; color: #dc3545;"></i>
                                </div>
                                <h6 class="card-title mb-0">${stock.name} (${stock.code})</h6>
                            </div>
                            <div class="stock-details d-flex flex-wrap" style="gap: 1rem;">
                                <div class="text-end" style="min-width: 100px;">
                                    <small class="text-muted d-block">매입가</small>
                                    <span class="fw-bold">${Math.round(stock.purchase_price).toLocaleString()}원</span>
                                </div>
                                <div class="text-end" style="min-width: 80px;">
                                    <small class="text-muted d-block">매입수량</small>
                                    <span class="fw-bold">${stock.purchase_quantity.toLocaleString()}주</span>
                                </div>
                                <div class="text-end" style="min-width: 100px;">
                                    <small class="text-muted d-block">현재가</small>
                                    <span class="fw-bold">${Math.round(stock.current_price).toLocaleString()}원</span>
                                </div>
                                <div class="text-end" style="min-width: 80px;">
                                    <small class="text-muted d-block">수익률</small>
                                    <span class="fw-bold ${returnClass}">${returnRate.toFixed(2)}%</span>
                                </div>
                                <div class="text-end" style="min-width: 100px;">
                                    <small class="text-muted d-block">총 평가금액</small>
                                    <span class="fw-bold">${totalValue.toLocaleString()}원</span>
                                </div>
                                <div class="text-end" style="min-width: 100px;">
                                    <small class="text-muted d-block">내재가치</small>
                                    <span class="fw-bold">${Math.round(stock.intrinsic_value).toLocaleString()}원</span>
                                </div>
                                <div class="text-end" style="min-width: 80px;">
                                    <small class="text-muted d-block">안전마진</small>
                                    <span class="fw-bold">${Math.round(stock.safety_margin)}%</span>
                                </div>
                                <div class="text-end" style="min-width: 80px;">
                                    <small class="text-muted d-block">배당수익률</small>
                                    <span class="fw-bold">${stock.dividend_yield.toFixed(2)}%</span>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            `;
        }).join('');

        watchlistContainer.innerHTML = html;
    }
}

// 전역 watchlistManager 인스턴스 생성
window.watchlistManager = new WatchlistManager();

// Initialize watchlist UI
document.addEventListener('DOMContentLoaded', () => {
    window.watchlistManager.updateWatchlistUI();
}); 