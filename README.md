# Demo Shop — Apple Store-style Vietnamese e-commerce landing

Trang web một-trang bán sản phẩm Apple (iPhone, iPad, Mac, Watch, AirPods, Phụ kiện) lấy cảm hứng visual từ apple.com kết hợp UX shop kiểu Cellphones / Thơ Mộng & Tinh Tế.

**Live demo:** https://demo-shop-gveawdhz.devinapps.com

## Stack

- HTML + Tailwind CSS (CDN) + custom CSS
- Vanilla JavaScript (không build step)
- Inter / SF Pro Display font
- [Remix Icon](https://remixicon.com/) cho icon set
- Tất cả ảnh sản phẩm Apple được tải về local trong `/img` (không phụ thuộc CDN ngoài)

## Tính năng

- Top promo strip cuộn (marquee) các thông điệp khuyến mãi
- Navbar mờ (blur) sticky kiểu apple.com
- 3 promo banner: iPhone 16 Series (đen + gold) · iPhone cũ giá sốc (cream warm + red) · Thu cũ đổi mới (ice cool + apple blue)
- Quick category nav với ảnh mockup sản phẩm đồng bộ
- Grid sản phẩm theo từng danh mục:
  - **iPhone** — 10 cards với filter chips (16 Pro / 16 / 15 / 14 / Máy cũ)
  - **iPad** — 5 cards
  - **Mac** — 5 cards
  - **Apple Watch** — 5 cards (mỗi card 1 phối màu khác nhau)
  - **AirPods** — 5 cards
- Mỗi card sản phẩm:
  - Badge **Giảm %** đỏ + **Trả góp 0%** xanh + **Bán chạy**
  - Tên — tình trạng (Chính hãng VN/A, Cũ Đẹp 99%, …)
  - Giá đỏ + giá gạch
  - 3 promo chip (Member / HSSV / Trả góp 0%)
  - Sao đánh giá + nút **Yêu thích** (toggle)
- Nút floating bottom-right: Lên đầu trang, Messenger, Zalo, Gọi (có pulse ring)
- Animation reveal khi scroll, hover ảnh card phóng nhẹ, drawer mobile
- Responsive 2/3/4/5 cột (mobile → desktop)

## Chạy local

Không cần build:

```bash
# Mở trực tiếp
xdg-open index.html        # Linux
open index.html            # macOS

# Hoặc serve qua Python
python3 -m http.server 8000
# rồi mở http://localhost:8000
```

## Cấu trúc

```
.
├── index.html        # Toàn bộ markup + JS render product grids
├── img/              # Ảnh sản phẩm Apple + ảnh banner/category
└── README.md
```
