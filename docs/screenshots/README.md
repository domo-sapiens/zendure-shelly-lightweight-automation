# Screenshots

Captured from the live system, not mocked up. Regenerate with:

```bash
tools/screenshots.sh http://zendure-log:8088
```

| File | Shows |
| --- | --- |
| `dashboard-overview.png` | Whole dashboard, 7-day window |
| `energy-flow.png` | KPI row and the live energy-flow diagram |
| `charts.png` | Grid power vs target, solar input, tracking error |
| `inverter-efficiency.png` | Measured efficiency curve and the fitted loss model |
| `mobile.png` | Narrow-screen layout |

All are cropped from a single capture so they show the same moment and cannot
drift out of sync. Widths are normalised to 1200 px to keep the repository light
and the README rendering consistent.

Nothing here is sensitive: the dashboard shows power readings and a tariff rate,
and no host addresses appear in the page itself.
