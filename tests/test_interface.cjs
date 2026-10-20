const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function node() {
    return {
        dataset: {}, attributes: {}, style: {}, children: [], textContent: '',
        setAttribute(key, value) { this.attributes[key] = value; },
        removeAttribute(key) { delete this.attributes[key]; },
        appendChild(child) { this.children.push(child); },
        replaceChildren(...children) { this.children = children; },
        querySelector() { return this.status; }
    };
}

(async () => {
    const elements = {};
    const get = id => elements[id] ||= node();
    for (const id of ['componente-evolucao', 'componente-gastos', 'componente-metas', 'fechamentoMesSection']) {
        get(id).status = node();
    }
    const charts = [];
    const media = { matches: true, addEventListener: (event, fn) => media.change = fn };
    const payloads = {
        '/api/dados_saldo': {labels: ['Outubro'], entradas: [123], saidas: [45]},
        '/api/dados_metas': {labels: ['Reserva <teste>'], atuais: [50], objetivos: [500]},
        '/api/fechamento_mes': null
    };
    const context = vm.createContext({
        document: {
            getElementById: get, createElement: node,
            documentElement: {getAttribute: () => 'light'}
        },
        window: {addEventListener() {}}, reducedMotion: media,
        Chart: function (canvas, config) {
            Object.assign(this, config);
            this.stop = () => {};
            this.update = () => {};
            this.destroy = () => {};
            charts.push(this);
        },
        fetch: async url => ({ok: url in payloads, status: 500, json: async () => payloads[url]})
    });
    const source = fs.readFileSync('templates/dashboard.html', 'utf8').split('<script>')[1].split('</script>')[0];
    vm.runInContext(source, context);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(get('componente-gastos').dataset.state, 'error');
    assert.equal(get('componente-evolucao').dataset.state, 'ready');
    assert.equal(get('componente-metas').dataset.state, 'ready');
    assert.equal(get('fechamentoMesSection').dataset.state, 'empty');
    assert.match(get('componente-gastos').status.textContent, /Não foi possível/);
    assert.equal(get('componente-evolucao').attributes['aria-busy'], undefined);
    assert.equal(charts.length, 2);
    assert.ok(charts.every(chart => chart.options.animation === false));
    const evolutionRow = get('resumo-evolucao').children[0].children[0].children[0];
    assert.equal(evolutionRow.children[0].textContent, 'Outubro');
    assert.match(evolutionRow.children[1].textContent, /Entradas:.*123,00; Saídas:.*45,00/);
    const goalRow = get('resumo-metas').children[0].children[0].children[0];
    assert.equal(goalRow.children[0].textContent, 'Reserva <teste>');
    assert.equal(goalRow.children[0].attributes.scope, 'row');
    assert.match(goalRow.children[1].textContent, /Atual:.*50,00; Objetivo:.*500,00/);
    assert.equal((source.match(/createElement\('ul'\)/g) || []).length, 0);
    const markup = fs.readFileSync('templates/dashboard.html', 'utf8');
    assert.equal((markup.match(/<details class="chart-values mt-2">/g) || []).length, 3);
    media.matches = false;
    media.change();
    assert.ok(charts.every(chart => chart.options.animation !== false));
    context.fetch = async () => ({ok: true, json: async () => ({labels: [], valores: []})});
    await vm.runInContext("carregarComponente('/api/dados_gastos', renderGastos, 'componente-gastos')", context);
    assert.equal(get('componente-gastos').dataset.state, 'empty');
    const form = node();
    const button = {disabled: false};
    const unavailable = {disabled: true};
    const events = {};
    const pageEvents = {};
    form.querySelectorAll = () => [button, unavailable];
    form.addEventListener = (name, fn) => events[name] = fn;
    const feedback = vm.createContext({
        document: {addEventListener() {}, getElementById: get},
        window: {addEventListener: (name, fn) => pageEvents[name] = fn}
    });
    vm.runInContext(fs.readFileSync('static/usability.js', 'utf8'), feedback);
    feedback.form = form;
    vm.runInContext('bindSubmitFeedback(form)', feedback);
    const cancelled = {defaultPrevented: true};
    events.submit(cancelled);
    assert.equal(button.disabled, false);
    let accepted = 0;
    for (let i = 0; i < 2; i++) {
        const event = {defaultPrevented: false, preventDefault() {this.defaultPrevented = true;}};
        events.submit(event);
        if (!event.defaultPrevented) accepted++;
    }
    assert.equal(accepted, 1);
    assert.equal(button.disabled, true);
    assert.equal(form.attributes['aria-busy'], 'true');
    assert.equal(get('async-status').textContent, 'Processando…');
    pageEvents.pageshow();
    assert.equal(button.disabled, false);
    assert.equal(unavailable.disabled, true);
    assert.equal(form.dataset.submitting, undefined);
    console.log('PASS: dashboard isolado, resumos textuais e preferência de movimento.');
    console.log('PASS: envio duplicado bloqueado e formulário restaurado ao voltar.');
})().catch(error => { console.error(error); process.exitCode = 1; });
