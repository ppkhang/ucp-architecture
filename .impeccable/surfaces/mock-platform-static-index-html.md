---
version: 1
slug: "mock-platform-static-index-html"
primary_target: "mock_platform/static/index.html"
related_targets: ["mock_platform/main.py"]
---

Mock shop UI. Mode: Operate (control panel, my orders), with a simple storefront and the sign-in and payment pages. Audience: demo viewers and Khang testing, switched by a toggle. English. Code-led.

## Direction contract

THESIS: Every order is a parcel waybill that you can read from across a room. It refuses the admin template of stat cards over a data table.

OWN-WORLD: Cream ground, a solid purple masthead, paper-panel waybills with a 2px ink frame and 1px box rules. Barlow Condensed for route codes, Barlow for text. Purple marks the current state, blue paid, orange failure or cancel. Test controls live only on a gold tear-off stub behind a perforation.

STORY: The viewer sees what the shop holds, which stage each order reached and who can act. A tester drives it from the stub.

FIRST VIEWPORT: Masthead with Store, My orders, Control and the Demo/Testing switch. Control opens on waybills in the wide column: large route code, buyer, total, lines, five stage boxes. Webhook log and test settings in the side column.

FORM: Parcel waybill system, candidate 3 of the grounded list. seed:71b69f3e632eba93

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
