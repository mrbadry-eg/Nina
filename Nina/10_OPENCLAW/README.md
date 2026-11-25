# OpenClaw Runtime Plane (Phase 3/6 Target — Unavailable in Phase 0)

Status: `unavailable` (see `08_TOOLS_MCP/integration_status.json`).

## Role Separation
- Owns customer messaging channel bridges (WhatsApp / Telegram) and interactive ReAct loops for `agent.support` (`ar-EG` default, `en` secondary).
- All tool calls route through `08_TOOLS_MCP/tool_gateway.py` (never direct DB/Shopify admin access).
- Cannot approve `L4` actions or execute `L5` prohibited actions.
