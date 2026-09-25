# cases/ manifest

- cases: 20 (HHG-001, HHG-002, HHG-003, HHG-004, HHG-005, HHG-006, HHG-007, HHG-008, HHG-009, HHG-010, HHG-011, HHG-012, HHG-013, HHG-014, HHG-015, HHG-016, HHG-017, HHG-018, HHG-019, HHG-020)
- runs: `live-final-2`; last promoted: `live-final-2`
- model: `claude-sonnet-5` (effort high), llm backend `cli`
- engine: `engine`
- git (per case below): `be035338a08c5fd960be2311786659bfb67ade55`
- prompt sha256: `assess.md`=ff8dd8b37336, `explain.md`=9580d7143059, `investigate.md`=a929aacba6f0, `nba.md`=67043e265092, `sar.md`=b68538088664, `system.md`=be81bd123234

| case | verdict | p | pattern | sar | tool_calls | tokens | latency_s | notes | run_id | git |
|---|---|---|---|---|---|---|---|---|---|---|
| HHG-001 | legitimate | 0.1 | none | False | 16 | 232932 | 131.7 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-002 | fraud | 0.92 | card_not_present_fraud | False | 17 | 238885 | 203.7 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-003 | uncertain | 0.6 | out_of_region_use | False | 17 | 308253 | 184.0 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-004 | uncertain | 0.6 | card_not_present_new_device | False | 20 | 305455 | 146.4 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-005 | legitimate | 0.03 | none | False | 20 | 244752 | 158.6 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-006 | fraud | 0.9 | undocumented | True | 24 | 294362 | 182.8 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-007 | fraud | 0.77 | account_takeover | False | 16 | 483760 | 228.5 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-008 | fraud | 0.92 | card_not_present_new_device | False | 18 | 310639 | 158.2 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-009 | fraud | 0.9 | card_not_present_fraud | False | 17 | 242452 | 166.7 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-010 | legitimate | 0.05 | none | False | 20 | 311261 | 252.6 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-011 | uncertain | 0.6 | card_not_present_new_device | False | 21 | 616283 | 203.9 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-012 | legitimate | 0.03 | none | False | 15 | 294239 | 112.8 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-013 | legitimate | 0.09 | none | False | 19 | 359953 | 157.8 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-014 | fraud | 0.9 | undocumented | True | 22 | 284222 | 128.0 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-015 | uncertain | 0.35 | account_takeover | False | 19 | 314210 | 192.9 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-016 | fraud | 0.77 | card_not_present_new_device | False | 21 | 264901 | 148.1 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-017 | legitimate | 0.1 | none | False | 20 | 239841 | 143.8 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-018 | uncertain | 0.55 | out_of_region_use | False | 17 | 418595 | 182.2 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-019 | fraud | 0.77 | card_not_present_new_device | False | 22 | 291206 | 180.6 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-020 | legitimate | 0.05 | none | False | 21 | 236845 | 142.6 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
