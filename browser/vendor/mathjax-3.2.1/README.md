# MathJax 3.2.1 browser resources

Unmodified files from the official `mathjax@3.2.1` npm distribution:
https://registry.npmjs.org/mathjax/-/mathjax-3.2.1.tgz

The existing `../mathjax-tex-svg.js` is byte-identical to `es5/tex-svg.js`.
This directory supplies **all 35 TeX extensions**, plus the UI, accessibility,
and speech resources used by its dynamic loader. SVG glyphs are in the existing
bundle; CommonHTML/font and alternative input bundles are not used by MathLab.
`browser/index.html` explicitly sets the loader's `mathjax` path here.

Upstream license: Apache-2.0, reproduced in `LICENSE`.
Hosting reference: https://docs.mathjax.org/en/v3.2/web/hosting.html

`SHA256SUMS` records the vendored resources and the matching main bundle.
