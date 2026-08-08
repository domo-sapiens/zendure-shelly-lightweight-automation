# Dashboard design notes

## Hover readout: is a charting library needed?

The question was whether crosshair-and-tooltip forces a dependency, trading away
the self-contained property. Conclusion: **no**, and the reasoning is worth
keeping because it also says when that answer would change.

### What a library actually sells you

Charting libraries are mostly scales, layout, animation, plugin architecture,
accessibility, and a dozen chart types. A nearest-point readout is none of that.
The whole feature is:

1. `pointermove` → x in canvas pixels
2. invert the x scale → a timestamp
3. binary search the timestamp array → nearest index
4. read each series at that index, draw a vertical line and dots, position a
   `<div>`

That is ~80 lines. It is not the part of a charting library that is hard.

**Crucially, snapping is by x only**, so the pointer never has to be near the
line — which is exactly the "don't make me hunt for pixels" requirement. A
library would not do it better; several do it worse by requiring a hit within N
pixels of the actual mark.

### The cost that is real, and how it is avoided

The genuine performance trap is redrawing the whole chart on every mouse move.
At 6 h of raw data that is ~4300 points × 3 series per event, which would feel
sluggish on a phone.

Solved with **two stacked canvases per chart**: a data layer redrawn only when
data changes, and a transparent overlay redrawn on pointer move carrying just a
line and a few dots. Hover work drops to roughly a hundred operations. This is
the standard technique and costs one extra element per chart.

### Where the load lands

Entirely in the browser. The Pi serves static HTML and JSON; it has no idea the
pointer moved. Hovering cannot add any load to the collector host, which was the
concern worth checking. Binary search is O(log n) — about 12 comparisons over a
day of raw data.

### The honest cost

- ~80 lines of JavaScript, plus an overlay canvas per chart.
- Pointer-event handling written by hand, including touch.
- No dependency, no vendored asset, no CDN, no version to keep current, and the
  page still works with no network in five years.

Against a library: 45–200 KB, an asset to vendor and re-vendor, and a supply
chain for a feature that is a page of code.

### When to revisit

If **drag-to-zoom, brush selection, or pan with inertia** is ever wanted, that
calculus changes — those involve real interaction state machines where a library
earns its size. Static hover readout does not.

## Chart scaling

Two decisions that are not obvious from the code:

**Robust y-scaling.** Household load is extremely peaky: a single kettle puts a
1232 W point in an hour whose median is 15 W, and true autoscaling then
compresses everything meaningful into one pixel. Charts scale to the 1st–99th
percentile and clip, then report how many points fell outside and what the true
range was — visible, not silent.

**The x-axis spans the requested window, not the data.** Asking for 24 h when
only 6 h exists shows 24 h with the earlier part empty. Scaling x to the data
would silently relabel a 6 h chart as if it were the 24 h that was asked for,
which is worse than an honest gap.

## Gaps are data

A failed poll is stored as a row with `shelly_ok`/`zendure_ok` = 0 and NULL
readings, not as a missing row. Charts break the line across NULLs rather than
interpolating. "The collector was down" and "nothing happened" must never look
the same.
