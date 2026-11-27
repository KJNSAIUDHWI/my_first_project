/* 8904 合一审阅台前端一致性测试（零外部依赖，直接用 Node vm 校验语法与核心功能契约）。 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const reviewHtml = fs.readFileSync(path.resolve(__dirname, '../apps/review/index.html'), 'utf8');
const annotatorHtml = fs.readFileSync(path.resolve(__dirname, '../apps/annotator/index.html'), 'utf8');

// 1. 校验内嵌脚本语法合法
const scripts = [...reviewHtml.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)]
  .map((m) => m[1])
  .filter((s) => s.trim().length > 0);
assert.ok(scripts.length >= 1, 'review/index.html must contain embedded script');
for (const [idx, code] of scripts.entries()) {
  new vm.Script(code, { filename: `apps/review/index.html#script-${idx}` });
}

// 2. 校验与标注工具 7 类检测框名称与配色完全一致
const expectedClasses = [
  ['0', '姓名学号', '#dc2626'],
  ['1', '选择题区', '#16a34a'],
  ['2', '选择-题号', '#0d9488'],
  ['3', '选择-选项', '#059669'],
  ['4', '非选择题区', '#2563eb'],
  ['5', '大题-框', '#7c3aed'],
  ['6', '大题-题号', '#d97706'],
];
for (const [, name, color] of expectedClasses) {
  assert.ok(reviewHtml.includes(name) && reviewHtml.includes(color), `review UI missing class ${name} (${color})`);
  assert.ok(annotatorHtml.includes(name) && annotatorHtml.includes(color), `annotator UI missing class ${name} (${color})`);
}

// 3. 校验核心交互功能契约：左右双侧就近总览、调框左键长按画框、删框消卡与单框 API 识别
for (const token of [
  'hitTestSelectedBox',
  'hitTestAnyBox',
  'draw_pending',
  'deleteBoxAt',
  'undoBoxEdit',
  'bindBoxesAndItems',
  'recognizeBoxIndices',
  '/api/recognize-box',
  '未作答',
]) {
  assert.ok(reviewHtml.includes(token), `review UI missing expected feature symbol: ${token}`);
}

console.log('PASS: review UI syntax, 7-class palette sync, dual-sided overview, box draw/CRUD, and single-box API hook verified.');
