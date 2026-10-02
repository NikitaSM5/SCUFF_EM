"""Shared, solver-independent definition of the planar voltage-gap source."""
MODEL = "planar_delta_gap_voltage"


def planar_feed(cells, cell_size_mm, feed_cell):
    n, a = len(cells), float(cell_size_mm)
    r, c = feed_cell
    x, y = (c + .5 - n/2)*a, (n/2-r-.5)*a
    return dict(model=MODEL, center_mm=[x, y, 0.0],
                start_mm=[x, y-a/2, 0.0], end_mm=[x, y+a/2, 0.0],
                width_mm=a, gap_mm=0.0, voltage_v=1.0,
                positive_side="+x", negative_side="-x",
                reference="differential_between_planar_halves")


def metal_rectangles(cells, cell_size_mm):
    # Half-cell partitions give both meshers the same conformal port seam.
    n, a = len(cells), float(cell_size_mm)
    return [dict(row=r, column=c, half=half, x=(c+half/2-n/2)*a,
                 y=(n/2-r-1)*a, width=a/2, depth=a)
            for r, row in enumerate(cells) for c, metal in enumerate(row)
            if metal for half in (0, 1)]
