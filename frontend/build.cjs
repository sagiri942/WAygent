// Deterministic local assets; Python deployments serve the committed output.
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const vm = require("node:vm");
const babel = require("@babel/core");
const root = __dirname;
const check = process.argv.includes("--check");
const sources = ["api.js", "chat-state.js", "navigation-state.js", "tweaks-panel.jsx", "mascot.jsx", "components.jsx", "edit.jsx", "pages.jsx", "detail-redesign.jsx", "main.jsx"];
const vendorFiles = {
  "react.js": "react/umd/react.production.min.js",
  "react-dom.js": "react-dom/umd/react-dom.production.min.js",
  "sortable.js": "sortablejs/Sortable.min.js",
  "gsap.js": "gsap/dist/gsap.min.js",
  "react-LICENSE.txt": "react/LICENSE",
  "react-dom-LICENSE.txt": "react-dom/LICENSE",
  "sortable-LICENSE.txt": "sortablejs/LICENSE",
  "gsap-package.json": "gsap/package.json",
};
const hash = value => crypto.createHash("sha256").update(value).digest("hex");
const sourceData = Object.fromEntries(sources.map(name => [name, fs.readFileSync(path.join(root, name), "utf8").replace(/\r\n/g, "\n")]));
const bundle = sources.map(name => babel.transformSync(sourceData[name], {
  filename: name, babelrc: false, configFile: false, sourceType: "script",
  plugins: [[require("@babel/plugin-transform-react-jsx"), { runtime: "classic" }]],
  comments: false, compact: true,
}).code).join(";\n") + "\n";
new vm.Script(bundle, { filename: "generated/app.js" });
const files = { "app.js": Buffer.from(bundle) };
for (const [name, source] of Object.entries(vendorFiles)) files[name] = fs.readFileSync(path.join(root, "node_modules", source));
const manifest = {
  sources: Object.fromEntries(Object.entries(sourceData).map(([name, data]) => [name, hash(data)])),
  files: Object.fromEntries(Object.entries(files).map(([name, data]) => [name, hash(data)])),
};
files["manifest.json"] = Buffer.from(JSON.stringify(manifest, null, 2) + "\n");
if (!check) fs.mkdirSync(path.join(root, "generated"), { recursive: true });
for (const [name, data] of Object.entries(files)) {
  const target = path.join(root, "generated", name);
  if (check) {
    if (!fs.existsSync(target) || !fs.readFileSync(target).equals(data)) throw new Error(`Stale asset ${name}; run npm run build in frontend`);
  } else fs.writeFileSync(target, data);
}
const htmlPath = path.join(root, "index.html");
const html = fs.readFileSync(htmlPath, "utf8");
const updated = html.replace(/(\/generated\/(?:app|react|react-dom|sortable|gsap)\.js)\?v=[a-zA-Z0-9-]+/g,
  (match, url) => `${url}?v=${manifest.files[path.basename(url)].slice(0, 16)}`);
if (check && html !== updated) throw new Error("Stale HTML asset versions; run npm run build in frontend");
if (!check) fs.writeFileSync(htmlPath, updated);
console.log(`${check ? "Verified" : "Built"} ${sources.length} sources and ${Object.keys(vendorFiles).length} vendor/license files`);
