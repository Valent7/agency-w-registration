/* Run with node tests/test_public_neona_widget.js. */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const source = fs.readFileSync(path.join(__dirname, '../free_tool_time/neona-chat.js'), 'utf8');
const begin = source.indexOf('  function referralCode()');
const end = source.indexOf('  async function ensureIdentity()', begin);
const saved = new Map();
const context = {
  URL,
  window: {location: {href: 'https://agency-w-time.onrender.com/?ref=W222222'}},
  sessionStorage: {
    setItem: (key, value) => saved.set(key, value),
    getItem: key => saved.get(key),
  },
  document: {
    getElementById: id => id === 'result' ? {style: {display: 'block'}} : {
      value: {m: 3, c: 2, s: 3, a: 2, r: 2}[id],
      closest: () => ({querySelector: () => ({textContent: id})}),
    },
  },
};
vm.createContext(context);
vm.runInContext(source.slice(begin, end), context);
assert.equal(context.referralCode(), 'W222222');
context.window.location.href = 'https://agency-w-time.onrender.com/';
assert.equal(context.referralCode(), 'W222222');
context.window.location.href = 'https://agency-w-time.onrender.com/?ref=W333333';
assert.equal(context.referralCode(), 'W333333');
context.window.location.href = 'https://agency-w-time.onrender.com/?ref=bad';
assert.equal(context.referralCode(), 'bad');
context.window.location.href = 'https://agency-w-time.onrender.com/?ref=';
assert.equal(context.referralCode(), 'invalid');
context.sessionStorage = {setItem: () => {throw Error('blocked');}};
context.window.location.href = 'https://agency-w-time.onrender.com/?ref=W222222';
assert.equal(context.referralCode(), 'W222222');
const calc = context.calculatedTime();
assert.equal(calc.weekly_hours, 12);
assert.equal(calc.monthly_hours, 52);
assert.equal(calc.tasks.length, 5);
assert.equal(calc.tasks[0].hours, 3);
console.log('PASS referral recovery, explicit referral precedence, blocked storage, calculation categories');
