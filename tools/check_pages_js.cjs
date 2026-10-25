/* Проверка синтаксиса инлайнового JS из HTML-страниц (док и оверлей).

   node tools\check_pages_js.cjs assets/subs/index.html assets/dock/index.html

   Страницы живут одним файлом с <script> внутри, поэтому их «компиляцию»
   нельзя проверить простым node --check — сначала вытаскиваем скрипт.
*/
const fs = require('fs');
const os = require('os');
const path = require('path');
const vm = require('vm');

const files = process.argv.slice(2);
if (!files.length) {
  console.error('использование: node tools/check_pages_js.cjs <файл.html> [...]');
  process.exit(2);
}

let failed = 0;
for (const file of files) {
  const html = fs.readFileSync(file, 'utf8');
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
  if (!scripts.length) {
    console.warn(`${file}: инлайновых <script> не найдено`);
    continue;
  }
  scripts.forEach((code, index) => {
    const label = `${file}${scripts.length > 1 ? ` [script ${index + 1}]` : ''}`;
    try {
      new vm.Script(code, { filename: label });
      console.log(`  ok  ${label}: ${code.split('\n').length} строк JS`);
    } catch (err) {
      failed += 1;
      console.error(` FAIL ${label}: ${err.message}`);
    }
  });
}

console.log(failed ? `ПРОВЕРА НЕ ПРОЙДЕНА: ${failed}` : 'JS OK');
process.exit(failed ? 1 : 0);
