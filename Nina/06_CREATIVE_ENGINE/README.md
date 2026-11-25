# Creative Content Engine (Phase 4 Target — Architecture Frozen)

Conforms strictly to `01_ARCHITECTURE/SILICIUM_CANONICAL_SPECIFICATION.md` Section 7 and schemas `12_creative_brief`, `13_creative_asset`, `14_creative_evaluation`, and `15_performance_observation`.

## Invariants
- **Creative Memory != Product Truth:** Historical creative copy/hooks must never assert current specs, prices, warranty, or stock.
- **Quality != Observed Performance:** Pre-publish `creative_evaluation` never replaces `performance_observation`.
- **Platform Nativeness:** Instagram Reel, Instagram Carousel, Instagram Story, Facebook, and TikTok require distinct structural outputs.
- **Approval Gate:** Zero autonomous social publishing (`L4` SHA-256 payload-bound Founder approval required).
