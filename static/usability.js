// Accessible labels and inline validation, retaining native validation as fallback.
function bindSubmitFeedback(form) {
    const controls = Array.from(form.querySelectorAll('button[type="submit"], button:not([type]), input[type="submit"]'));
    const initialDisabled = controls.map(control => control.disabled);
    form.addEventListener('submit', event => {
        if (event.defaultPrevented) return;
        if (form.dataset.submitting === 'true') {
            event.preventDefault();
            return;
        }
        form.dataset.submitting = 'true';
        form.setAttribute('aria-busy', 'true');
        controls.forEach(control => control.disabled = true);
        const status = document.getElementById('async-status');
        if (status) status.textContent = 'Processando…';
    });
    window.addEventListener('pageshow', () => {
        delete form.dataset.submitting;
        form.removeAttribute('aria-busy');
        controls.forEach((control, index) => control.disabled = initialDisabled[index]);
    });
}

function bindCurrencyInput(field) {
    const format = () => {
        const digits = field.value.replace(/\D/g, '');
        field.value = digits ? (Number(digits) / 100).toLocaleString('pt-BR', {
            minimumFractionDigits: 2, maximumFractionDigits: 2
        }) : '';
        const allowZero = field.dataset.currencyMin === '0';
        field.setCustomValidity(digits && Number(digits) === 0 && !allowZero ? 'Informe um valor maior que zero.' : '');
    };
    const prefill = () => {
        if (field.value && !field.value.includes(',')) field.value = Number(field.value).toFixed(2);
        format();
    };
    prefill();
    field.addEventListener('input', format);
    field.addEventListener('change', prefill);
    field.form.addEventListener('formdata', event => {
        event.formData.set(field.name, field.value.replace(/\./g, '').replace(',', '.'));
    });
}

document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-currency-input]').forEach(bindCurrencyInput);
    document.querySelectorAll('form[data-submit-feedback]').forEach(bindSubmitFeedback);
    const menu = document.getElementById('maisMenuOffcanvas');
    const menuButton = document.querySelector('[aria-controls="maisMenuOffcanvas"]');
    if (menu && menuButton) {
        menu.addEventListener('shown.bs.offcanvas', () => menuButton.setAttribute('aria-expanded', 'true'));
        menu.addEventListener('hidden.bs.offcanvas', () => menuButton.setAttribute('aria-expanded', 'false'));
    }
    document.querySelectorAll('form').forEach((form, formIndex) => {
        form.querySelectorAll('input:not([type=hidden]), select, textarea').forEach((field, index) => {
            if (!field.id) field.id = `field-${formIndex}-${index}`;
            const label = field.parentElement.querySelector('label');
            if (label && !label.htmlFor) label.htmlFor = field.id;
            if (field.type === 'checkbox' || field.type === 'file') return;
            const error = document.createElement('div');
            error.className = 'field-error';
            error.id = `${field.id}-error`;
            error.hidden = true;
            field.insertAdjacentElement('afterend', error);
            const previousDescription = field.getAttribute('aria-describedby') || '';
            field.setAttribute('aria-describedby', `${previousDescription} ${error.id}`.trim());
            field.addEventListener('invalid', () => {
                const details = field.closest('details');
                if (details) details.open = true;
                field.setAttribute('aria-invalid', 'true');
                error.textContent = field.validity.valueMissing ? 'Preencha este campo para continuar.' : field.validationMessage;
                error.hidden = false;
            });
            field.addEventListener('input', () => {
                if (field.validity.valid) {
                    field.removeAttribute('aria-invalid');
                    error.hidden = true;
                }
            });
        });
    });
    document.querySelectorAll('input[type=file]').forEach(input => {
        const status = document.createElement('p');
        status.className = 'form-text';
        status.setAttribute('role', 'status');
        input.insertAdjacentElement('afterend', status);
        input.addEventListener('change', () => {
            status.textContent = input.files.length ? `Selecionado: ${input.files[0].name}` : '';
        });
    });
    // Keyboard navigation across tabs (ArrowRight / ArrowLeft / Home / End)
    document.querySelectorAll('[role="tablist"]').forEach(tablist => {
        const tabs = Array.from(tablist.querySelectorAll('[role="tab"]:not([disabled])'));
        tabs.forEach((tab, index) => {
            tab.addEventListener('keydown', (event) => {
                let targetIndex = null;
                if (event.key === 'ArrowRight') {
                    event.preventDefault();
                    targetIndex = (index + 1) % tabs.length;
                } else if (event.key === 'ArrowLeft') {
                    event.preventDefault();
                    targetIndex = (index - 1 + tabs.length) % tabs.length;
                } else if (event.key === 'Home') {
                    event.preventDefault();
                    targetIndex = 0;
                } else if (event.key === 'End') {
                    event.preventDefault();
                    targetIndex = tabs.length - 1;
                }
                if (targetIndex !== null) {
                    tabs[targetIndex].focus();
                    tabs[targetIndex].click();
                }
            });
        });
    });
});
