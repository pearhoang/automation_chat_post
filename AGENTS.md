# Working agreements

- Khi giao tiếp, trả lời, walkthrough, task/checklist, hướng dẫn triển khai: viết tiếng Việt.
- Giữ nguyên tiếng Anh cho: tên hàm/biến, log lỗi, lệnh terminal, config key, API field.
- Luôn phân loại nhiệm vụ thành `Quick Task` hoặc `Project Task` trước khi thực hiện.
- Mục tiêu ưu tiên: giảm token đầu vào, chỉ nạp context theo nhu cầu thực tế.

## Quick Task

- Trả lời trực tiếp.
- Không đọc project memory mặc định.
- Chỉ đọc code/file khi câu hỏi phụ thuộc trực tiếp vào chúng.
- Không bắt buộc cập nhật `docs/CHANGELOG.md`, `docs/DECISIONS_INDEX.md`, `docs/modules/*`, `docs/tasks/*`.

## Project Task

- Trước khi sửa code, luôn đọc:
  - `AGENTS.md`
  - `docs/PROJECT_BRIEF.md`
  - `docs/MEMORY_INDEX.md`
- Chỉ đọc thêm khi task thực sự cần.
- Sau mỗi `Project Task`, append 1 entry ngắn vào `docs/CHANGELOG.md`.
- Khi có quyết định mới còn hiệu lực, cập nhật `docs/DECISIONS_INDEX.md` và `docs/DECISIONS.md`.

## Project Memory

- Với mọi `Project Task`, ưu tiên dùng skill `project-memory-bootstrap` nếu khả dụng.
- `docs/PROJECT_BRIEF.md` là canonical source cho mục tiêu, kiến trúc, build/test/lint, invariant toàn cục.
- `docs/MEMORY_INDEX.md` là routing file cho memory cần đọc thêm.
- Không duplicate cùng một thông tin ở nhiều file memory.

## UI Design Discipline

- Task UI luôn dùng skill `uncodixfy`.
- Nếu đụng product UI/dashboard/settings/detail page thì dùng thêm `tailwind-ai-webapp-ui`.
- Nếu thiên về palette/contrast/surface/border/shadow/motion thì dùng thêm `ui-color-visual-language`.
- Nếu review/audit/responsive/accessibility/UX regression thì dùng thêm `frontend-ui-audit`.
- Nếu có tiếng Việt trong UI thì dùng thêm `utf8-vietnamese-ui-guard`.

## Safety Rules

- Không đổi API/contract nếu chưa xác nhận impact hoặc chưa có migration plan.
- Không dùng memory file để thay cho việc đọc code liên quan.
- Không commit secret, token, password, `.env`, log, hay dữ liệu runtime của khách.
- Không revert thay đổi không do mình tạo.
