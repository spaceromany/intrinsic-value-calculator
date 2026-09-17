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

}

// 전역 watchlistManager 인스턴스 생성
window.watchlistManager = new WatchlistManager();
