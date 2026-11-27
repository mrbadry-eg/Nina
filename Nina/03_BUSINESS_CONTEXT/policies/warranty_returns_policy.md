# Warranty and Returns Policy

- **Policy ID:** POL-WARRANTY-RETURNS-001
- **Version:** 0.1.0
- **Status:** PROPOSED
- **Authority level:** Level 2 `OFFICIAL_SILICIUM_POLICY` once approved by `founder:abdulrahman`
- **Derived from:** `founder_context.json` (`refunds_credits_compensation` is always Founder-owned; refunds are L5), `business_context.json` (`prohibited_practices`), `SILICIUM-SPEC-001` section 3.3

This document contains **no invented numbers**. Every value the business has not yet decided is listed under *Open decisions* and stays `UNSET` until the Founder sets it. Agents must treat an `UNSET` value as a policy gap and escalate.

## Rules

| Rule | Statement |
| :--- | :--- |
| WR-01 | **Statutory floor.** Applicable Egyptian consumer-protection law (Level 1) always prevails. No Silicium policy may give a customer less than the statutory minimum. If policy text and law conflict, the law wins and the conflict is logged as a discrepancy. |
| WR-02 | **Warranty source.** Warranty terms for a product are read only from that SKU's verified `product_truth.warranty_terms` (Level 4). If the field is null or missing, say the terms are not confirmed and escalate. Never infer warranty from brand, category or a similar SKU. |
| WR-03 | **Provenance.** Keep manufacturer warranty and any Silicium-provided support distinct, exactly as recorded in `product_truth`. |
| WR-04 | **Eligibility inputs.** Return or exchange eligibility is assessed from the live order record (Level 3: fulfillment status and dates), this policy, and statutory rights. Until OD-WR-1 is set, agents state no day-count and escalate. |
| WR-05 | **No autonomous promises.** Agents never promise or imply a refund, credit, replacement, exception or compensation. They may explain approved policy and draft a recommendation for the Founder. |
| WR-06 | **Refunds are Level 5.** Only the Founder executes refunds or credits, in Shopify. The Tool Gateway blocks `shopify.orders.refund` unconditionally. |
| WR-07 | **Recommendation package.** A draft recommendation contains: Shopify order ID; order date and delivery status (Level 3); payment method; SKU and warranty terms (Level 4); the customer's stated issue quoted as untrusted; any evidence supplied; applicable rule IDs; and the recommended action labelled `recommendation`. |
| WR-08 | **Defect claims.** A customer-reported defect is a customer statement until inspected. Agents do not confirm a fault or assign blame. |
| WR-09 | **Tone and language.** Follow the Brand Bible: `ar-EG` by default, acknowledge, state known facts, say what happens next and who decides. |

## Escalation

Escalate to `founder:abdulrahman` when: a required value is `UNSET`; warranty terms are missing; the customer asks for any refund, credit, replacement or exception; the law and this policy appear to conflict.

## Open decisions (Tier 0 must set)

| ID | Decision | Value |
| :--- | :--- | :--- |
| OD-WR-1 | Return and exchange window (days from delivery) | UNSET |
| OD-WR-2 | Who pays return shipping, by reason (defect vs change of mind) | UNSET |
| OD-WR-3 | Inspection and restocking conditions for returned goods | UNSET |
| OD-WR-4 | How manufacturer warranty vs Silicium-handled warranty is communicated per category | UNSET |
| OD-WR-5 | Statutory minimums confirmed with counsel | UNSET |

## Change control

Only Tier 0 can approve this policy. Approval is recorded in `policy_index.json` (`status`, `approved_by`, `approved_at`) through a pull request.
