/* Проверка логики оверлея субтитров без браузера: stub DOM + реальный код страницы.
   Запуск:  node tools\test_overlay.cjs assets\subs\index.html                       */
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync(process.argv[2] || 'assets/subs/index.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function el(tag) {
  const node = {
    tag, className: '', textContent: '', children: [], style: { setProperty() {} },
    classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
    addEventListener() {},
  };
  node.appendChild = (c) => node.children.push(c);
  node.replaceChildren = (...nodes) => {
    node.children.length = 0;
    nodes.filter(Boolean).forEach((n) => node.children.push(n));
  };
  return node;
}

let currentQuery = '';
const stage = el('stage');

function boot() {
  const sandbox = {
    console,
    window: {},
    setTimeout: () => 0,
    clearTimeout: () => {},
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: {
      documentElement: el('html'),
      body: el('body'),
      getElementById: () => stage,
      createElement: (tag) => el(tag),
      addEventListener() {},
      hidden: false,
    },
    location: { get search() { return currentQuery; }, protocol: 'http:', host: '127.0.0.1:1' },
    URLSearchParams,
    WebSocket: function FakeSocket() { this.close = () => {}; this.send = () => {}; },
  };
  vm.createContext(sandbox);
  vm.runInContext(script, sandbox);
  if (!sandbox.window.govorokSubs) throw new Error('у страницы нет точки входа govorokSubs');
  return sandbox;
}

function render(mode) {
  currentQuery = `?mode=${mode}&lines=3&font=28&chars=60&hold=4&pos=bottom`;
  stage.children.length = 0;
  const sandbox = boot();
  const subs = sandbox.window.govorokSubs;
  subs.handle({ type: "subtitle", id: "1", final: true, text: "Привет, чат",
                original: "Привет, чат", ts: 1 });
  subs.handle({ type: "subtitle", id: "2", final: true, text: "Сегодня играем в Доту",
                original: "Сегодня играем в Доту", ts: 2 });
  // пакеты соответствуют тому, что реально шлёт движок в этом режиме
  if (mode === "both") {
    subs.handle({ type: "subtitle_translation", id: "1", text: "Hello, chat", ts: 3 });
  } else if (mode === "en") {
    subs.handle({ type: "subtitle", id: "1", final: true, text: "Hello, chat",
                  original: "Привет, чат", replace: true, lang: "en", ts: 3 });
  }
  return {
    detected: subs.cfg.mode,
    lines: stage.children.map((line) => ({
      main: (line.children.find((c) => c.className === "text") || {}).textContent,
      sub: (line.children.find((c) => c.className === "trans") || {}).textContent || null,
    })),
  };
}

const out = {};
for (const mode of ["ru", "en", "both"]) {
  out[mode] = render(mode);
}
console.log(JSON.stringify(out, null, 1));

const ru = out.ru, en = out.en, both = out.both;
const ok =
  ru.detected === "ru" && ru.lines[0] && ru.lines[0].main === "Привет, чат" && !ru.lines[0].sub
  && en.detected === "en" && en.lines[0] && en.lines[0].main === "Hello, chat" && !en.lines[0].sub
  && both.detected === "both" && both.lines[0] && both.lines[0].main === "Привет, чат"
      && both.lines[0].sub === "Hello, chat";
console.log(ok ? "OVERLAY MODE LOGIC OK" : "OVERLAY MODE LOGIC FAIL");
process.exit(ok ? 0 : 1);
