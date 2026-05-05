---
name: inventory
description: Quản lý kho hàng điện thoại Apple cũ — thêm/xoá/tìm sản phẩm, lưu ảnh + thông số trong workspace, đồng bộ catalog cho auto-reply Messenger/comment.
---

# Inventory Management Skill

## Purpose
Quản lý kho hàng điện thoại cũ của Apple Shop Siêu Lướt. Mỗi sản phẩm gồm
mã (code), thông số (model/storage/màu/pin/BH/giá), ảnh ngoại quan, trạng
thái (in_stock / reserved / sold). Catalog được fb-webhook đọc để trả lời
khách realtime: "còn iPhone 14 Pro Max 1TB không?" → biết chính xác có hay không.

## Best fit
- Chủ shop gửi ảnh + caption "iPhone 14 PM 1TB Gold pin 100% BH 1/10/26 22.5tr" → agent thêm vào kho
- Chủ shop nói "bán rồi mã IP14PM-1TB-TIT-001" → agent đánh dấu sold
- Khách hỏi "còn iPhone 14 PM không?" → agent (hoặc fb-webhook) tra catalog → trả lời cụ thể
- Cần xem tổng kho / kiểm tra hàng còn

## Not a fit
- Quản lý đơn hàng (order management) — đây chỉ track inventory, không track customer/order
- Quản lý phụ kiện đa SKU (cable/case/sạc) — schema này tối ưu cho điện thoại
- Thanh toán / công nợ

## Folder layout

```
inventory/
├── INDEX.md           ← markdown overview, agent đọc trước khi tư vấn
├── catalog.json       ← machine-readable, fb-webhook đọc cho auto-reply
├── products/
│   └── {CODE}/
│       ├── info.json
│       ├── 01.jpg     ← main photo
│       ├── 02.jpg
│       └── ...
└── sold/              ← archive products đã bán (giữ history)
    └── {CODE}/
```

## Product schema (`info.json`)

```json
{
  "code": "IP14PM-1TB-TIT-001",
  "model": "iPhone 14 Pro Max",
  "storage": "1TB",
  "color": "Titan Gold",
  "condition": "Cũ - đẹp 99%",
  "battery": "100%",
  "warranty_until": "2026-10-01",
  "price_vnd": 22500000,
  "status": "in_stock",
  "added_at": "2026-04-27T15:00:00Z",
  "sold_at": null,
  "notes": "Đầy đủ phụ kiện zin",
  "tags": ["iphone", "14promax", "1tb", "titan"],
  "photo_count": 3
}
```

`status` ∈ {`in_stock`, `reserved`, `sold`}.

## Code format (auto-generated)

`{MODEL_SHORT}-{STORAGE}-{COLOR_SHORT}-{NNN}` — e.g. `IP14PM-1TB-TIT-001`.

Helpers in `scripts/codegen.py`:
- iPhone 14 Pro Max → `IP14PM`
- iPhone 15 → `IP15`
- iPhone 13 mini → `IP13MINI`
- color: "Titan Gold" → `TIT`, "Đen" → `BLK`, "Xanh" → `BLU`, …

`NNN` = sequential per (model, storage, color) combination.

## Tools

All scripts live in `inventory/scripts/`. Run with absolute paths:

### Add product
```bash
python3 ~/.openclaw/workspace/skills/inventory/scripts/add_product.py \
  --model "iPhone 14 Pro Max" \
  --storage "1TB" \
  --color "Titan Gold" \
  --battery "100%" \
  --warranty-until "2026-10-01" \
  --price 22500000 \
  --condition "Cũ - đẹp 99%" \
  --notes "Đầy đủ phụ kiện zin" \
  --photos /tmp/uploaded/img1.jpg /tmp/uploaded/img2.jpg
```
→ creates `inventory/products/IP14PM-1TB-TIT-NNN/` with `info.json` + numbered photos, regenerates INDEX.md + catalog.json.

### Mark sold (REVERSIBLE — moves to sold/, doesn't delete)
```bash
python3 .../scripts/mark_sold.py --code IP14PM-1TB-TIT-001
```

### Reserve (khách đặt cọc, chưa giao)
```bash
python3 .../scripts/set_status.py --code IP14PM-1TB-TIT-001 --status reserved
```

### Find product (free-text query)
```bash
python3 .../scripts/find_product.py "iPhone 14 PM 1TB"
```
→ returns matching products as JSON list (status filter: in_stock by default,
add `--all` to include sold).

### List all
```bash
python3 .../scripts/list_products.py             # in_stock only
python3 .../scripts/list_products.py --all       # include sold
python3 .../scripts/list_products.py --json      # raw JSON
```

### Rebuild index (idempotent, run after manual edits)
```bash
python3 .../scripts/rebuild_index.py
```

## When to call which tool

| User says | Tool |
|---|---|
| "Về 1 lô [model] [specs]" + sends photos | `add_product` (one per phone) |
| "Bán rồi [code]" / "Đã bán [code]" | `mark_sold --code <CODE>` |
| "Khách đặt cọc [code]" | `set_status --code <CODE> --status reserved` |
| "Hủy đặt cọc [code]" | `set_status --code <CODE> --status in_stock` |
| "Còn [model] [specs] không?" | `find_product "<query>"` |
| "Kho còn gì?" | `list_products` |

## ⚠️ DESTRUCTIVE GUARDRAILS

- `mark_sold` is reversible (move into `sold/`, no data loss). Always
  prefer this over deleting a product folder.
- NEVER `rm -rf` any product folder. If user really wants to purge a
  product, ask explicit confirmation by code: "Xác nhận xoá vĩnh viễn
  IP14PM-1TB-TIT-001? Gõ 'xoá vĩnh viễn IP14PM-1TB-TIT-001' để xác nhận."
- NEVER bulk-mark-sold across multiple products without per-item
  confirmation.

## Tone for customer-facing replies (referenced by fb-webhook)

When the agent or fb-webhook generates a reply that quotes inventory:
- Keep it natural like the shop owner texting (1-2 sentences max)
- Use "bạn", avoid "thưa anh/chị" / "quý khách"
- Don't append signatures like "— Demo Shop"
- Call out price + key specs + BH; invite inbox to chốt
- Example: *"Còn nhé, 14 Pro Max 1TB Titan Gold pin 100% BH 1/10/26, 22.5tr. Inbox shop chốt nha bạn 📱"*

## Data sources

- `inventory/catalog.json` — single source of truth (machine-readable).
  Format: `{"version":1,"products":[{...info.json...},...]}`. Read by
  fb-webhook on each customer message to inject in-stock context into the
  LLM prompt.
- `inventory/INDEX.md` — human-readable mirror of catalog.json. Agent
  reads this before answering inventory questions; faster than scanning
  every product folder.

Both are auto-regenerated by `add_product`, `mark_sold`, `set_status`,
`rebuild_index`. Do NOT hand-edit — always go through the scripts.
