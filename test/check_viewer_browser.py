"""Visual checks for the same local Three.js canvas embedded by Qt."""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("comparison_dir")
    args = parser.parse_args()
    directory = Path(args.comparison_dir).resolve()
    data = json.loads((directory / "comparison.json").read_text(encoding="utf-8"))
    pattern = json.loads((directory / "feko/farfield.json").read_text(encoding="utf-8"))[0]
    output = ROOT / "results/viewer-check"
    output.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True,
                                              args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        for width, height in ((1180, 740), (390, 700)):
            page.set_viewport_size(dict(width=width, height=height))
            page.goto((ROOT / "gui/web/viewer.html").as_uri())
            page.wait_for_function("window.viewerReady === true")
            page.evaluate("data => window.setScene(data)", dict(project=data["project"], pattern=pattern,
                          show_antenna=True, show_pattern=True, scale=0,
                          feed_model=data["feed_models"]["feko"]))
            page.wait_for_timeout(300)
            info = page.evaluate("window.viewerInfo()")
            assert info["calls"] > 5 and info["patternPoints"] > 100, info
            assert info["feedModel"] == data["feed_models"]["feko"]
            pixels = page.evaluate("""() => {
                const c=document.querySelector('canvas'), g=c.getContext('webgl2') || c.getContext('webgl');
                const p=new Uint8Array(g.drawingBufferWidth*g.drawingBufferHeight*4);
                g.readPixels(0,0,g.drawingBufferWidth,g.drawingBufferHeight,g.RGBA,g.UNSIGNED_BYTE,p);
                let colored=0; for(let i=0;i<p.length;i+=4) if(Math.max(p[i],p[i+1],p[i+2])-Math.min(p[i],p[i+1],p[i+2])>25) colored++;
                return {colored,total:p.length/4};
            }""")
            assert pixels["colored"] > pixels["total"]*.01, pixels
            page.screenshot(path=str(output / f"pattern-{width}.png"))
            before = page.locator("canvas").screenshot()
            page.mouse.move(width*.5, height*.5)
            page.mouse.down()
            page.mouse.move(width*.72, height*.62, steps=16)
            page.mouse.up()
            page.wait_for_timeout(250)
            assert page.evaluate("window.viewerInfo().camera") != info["camera"]
            assert page.locator("canvas").screenshot() != before
            page.evaluate("data => window.setScene(data)", dict(project=data["project"], pattern=pattern,
                          show_antenna=True, show_pattern=False, scale=0,
                          feed_model=data["feed_models"]["feko"]))
            page.screenshot(path=str(output / f"antenna-{width}.png"))
            print(width, height, pixels, "rotation OK")
        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    main()
