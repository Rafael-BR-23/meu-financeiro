const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

(async () => {
    const handlers = {};
    const origin = 'https://financeiro.example';
    vm.runInNewContext(fs.readFileSync('static/sw.js', 'utf8'), {
        self: { location: { origin }, addEventListener: (name, fn) => handlers[name] = fn },
        URL,
        fetch: () => assert.fail('POST deve seguir pelo navegador, sem GET duplicado pelo worker')
    });
    const body = new FormData();
    body.append('comprovante', new File(['receipt bytes'], 'teste.png', { type: 'image/png' }));
    body.append('text', 'Texto complementar');
    const request = new Request(origin + '/compartilhar-comprovante', { method: 'POST', body });
    handlers.fetch({
        request,
        respondWith: () => assert.fail('Não deve interceptar o compartilhamento')
    });
    assert.equal(request.bodyUsed, false);
    const received = await request.formData();
    assert.equal(await received.get('comprovante').text(), 'receipt bytes');
    assert.equal(received.get('comprovante').name, 'teste.png');
    assert.equal(received.get('text'), 'Texto complementar');
    handlers.fetch({ request: new Request(origin + '/pendencias/nova', { method: 'POST' }),
        respondWith: () => assert.fail('Não deve interceptar outros formulários') });
    for (const path of ['/', '/login', '/cadastro', '/auth/google/callback', '/api/dados_saldo']) {
        handlers.fetch({ request: new Request(origin + path),
            respondWith: () => assert.fail('Nunca deve recuperar páginas privadas ou autenticação do cache') });
    }
    const manifest = JSON.parse(fs.readFileSync('static/manifest.json', 'utf8'));
    assert.equal(manifest.share_target.method, 'POST');
    assert.equal(manifest.share_target.enctype, 'multipart/form-data');
    const accepts = manifest.share_target.params.files[0].accept;
    for (const type of ['application/octet-stream', 'application/pdf', '.pdf', '.png', '.jpg', '.jpeg', '.webp']) {
        assert.ok(accepts.includes(type), `Deve receber comprovante identificado por ${type}`);
    }
    const appSource = fs.readFileSync('app.py', 'utf8');
    assert.ok(appSource.includes("SHARE_ACCOUNT_LIMIT='10 per 10 minutes'"),
              'SHARE_ACCOUNT_LIMIT deve ser exatamente "10 per 10 minutes"');
    assert.ok(appSource.includes("SHARE_IP_LIMIT='30 per hour'"),
              'SHARE_IP_LIMIT deve ser exatamente "30 per hour"');
    assert.ok(appSource.includes('csrf.exempt(compartilhar_comprovante)'),
              'Rota Share Target deve permanecer CSRF-exempt');
    const dashSource = fs.readFileSync('templates/dashboard.html', 'utf8');
    assert.ok(dashSource.includes('animation: reducedMotion.matches ? false : undefined'),
              'Chart.js deve respeitar reducedMotion');
    console.log('PASS: compartilhamento preserva multipart original e aceita MIME genérico/extensões.');
})();
