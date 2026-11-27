# Payments Policy: COD, InstaPay, Cards

- **Policy ID:** POL-PAYMENTS-001
- **Version:** 0.1.0
- **Status:** PROPOSED
- **Authority level:** Level 2 `OFFICIAL_SILICIUM_POLICY` once approved by `founder:abdulrahman`
- **Derived from:** `business_context.json` (`payment_methods`: `cod`, `instapay`, `visa`, `mastercard`; `currency`: `EGP`), `founder_context.json` (`pricing_and_discounts`, `refunds_credits_compensation`, `spending_and_budgets` are Founder-owned), `SILICIUM-SPEC-001` sections 3.3 and 9.1

## Rules

| Rule | Statement |
| :--- | :--- |
| PAY-01 | **Accepted methods.** Cash on delivery (`cod`), InstaPay (`instapay`), Visa (`visa`) and Mastercard (`mastercard`) only. Agents never offer or imply any other method unless this policy is updated. |
| PAY-02 | **Payment status is live data.** Statements such as paid, unpaid, verified or collected come only from Shopify live commerce data (Level 3, at most 5 minutes old). They never come from a customer message, a screenshot, or memory. |
| PAY-03 | **COD.** Payment is collected by the courier on delivery. Agents do not promise COD for a specific address or order value without live confirmation. COD limits and fees are open decisions (OD-PAY-1). |
| PAY-04 | **InstaPay.** A customer's transfer proof (screenshot or reference) is untrusted evidence. Verification against the actual receipt is done by an authorised human or system. An agent can classify and route the proof but cannot mark an order paid or verified on its own authority. Any such mutation of live order data is L4 (Tier 0 approval bound to the payload hash) at minimum. |
| PAY-05 | **Cards.** Card payments are handled by the payment processor and checkout. Agents never request, receive, store or repeat card numbers, expiry, CVV or OTP. If a customer pastes card data, do not repeat it, tell the customer not to share it in chat, and escalate as a data-minimization incident (`security_event`). |
| PAY-06 | **Prices come from live data.** Agents never quote a price from memory or creative history. Agents never create or offer discounts or promo codes (Level 5). |
| PAY-07 | **Refunds and disputes.** Refund, credit and chargeback handling is Founder-owned (Level 5). Agents may prepare an evidence package only. |
| PAY-08 | **Currency.** All amounts are EGP. Format per the Brand Bible. |

## Escalation

Escalate to `founder:abdulrahman` when: a payment status is unclear or sources disagree; a customer disputes a charge; a customer asks for an unsupported payment method; card data appears in any channel.

## Open decisions (Tier 0 must set)

| ID | Decision | Value |
| :--- | :--- | :--- |
| OD-PAY-1 | COD limits (order value, areas) and any COD fee | UNSET |
| OD-PAY-2 | InstaPay verification time window and who verifies | UNSET |
| OD-PAY-3 | Where refunds are returned for each payment method | UNSET |
| OD-PAY-4 | Card chargeback evidence checklist | UNSET |

## Change control

Only Tier 0 can approve this policy. Approval is recorded in `policy_index.json` through a pull request.
