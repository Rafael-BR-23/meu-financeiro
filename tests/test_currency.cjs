const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const context = vm.createContext({document: {addEventListener() {}}});
vm.runInContext(fs.readFileSync('static/usability.js', 'utf8'), context);
function field(value = '', allowZero = false) {
    const events = {}, formEvents = {};
    const input = {value, dataset: {currencyMin: allowZero ? '0' : undefined}, name: 'valor', setCustomValidity(message) {this.error = message;},
        addEventListener(name, fn) {events[name] = fn;},
        form: {addEventListener(name, fn) {formEvents[name] = fn;}}};
    context.field = input;
    vm.runInContext('bindCurrencyInput(field)', context);
    return {input, events, formEvents};
}
const {input, events, formEvents} = field();
for (const digit of '1234') { input.value += digit; events.input(); }
assert.equal(input.value, '12,34');
const data = new Map();
formEvents.formdata({formData: data});
assert.equal(data.get('valor'), '12.34');
input.value = 'R$ 1.234,56'; events.input();
assert.equal(input.value, '1.234,56');
formEvents.formdata({formData: data});
assert.equal(data.get('valor'), '1234.56');
input.value = input.value.slice(0, -1); events.input();
assert.equal(input.value, '123,45');
input.value = ''; events.input(); assert.equal(input.value, '');
input.value = '0'; events.input(); assert.ok(input.error);
input.value = '85.32'; events.change();
assert.equal(input.value, '85,32'); assert.equal(input.error, '');
assert.equal(field('1234.50').input.value, '1.234,50');
const savings = field('0', true);
assert.equal(savings.input.value, '0,00');
assert.equal(savings.input.error, '');
const savingsData = new Map();
savings.formEvents.formdata({formData: savingsData});
assert.equal(savingsData.get('valor'), '0.00');
// Every monetary field must use the shared mask; counts/days retain native inputs.
const monetaryNames = new Set(['valor', 'valor_objetivo', 'valor_atual', 'proximo_valor']);
let covered = 0;
for (const file of fs.readdirSync('templates').filter(name => name.endsWith('.html'))) {
    const markup = fs.readFileSync(`templates/${file}`, 'utf8');
    for (const match of markup.matchAll(/<input\b[^>]*>/g)) {
        const name = match[0].match(/\bname="([^"]+)"/);
        if (!name || !monetaryNames.has(name[1])) continue;
        assert.match(match[0], /data-currency-input/, file);
        covered++;
    }
}
assert.equal(covered, 9);
console.log('PASS: moeda durante digitação, apagar, colar, comprovante, zero e envio decimal.');
