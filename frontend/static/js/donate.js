// "Donate for the Dackel" dialog (FIX-UI-9). Static only: no network access,
// no storage, no images. The Lightning address is shown, copied locally and
// offered as a lightning: link that only opens the local wallet app.
window.Donate = (function () {
  var ADDRESS = 'dackelmann@cake.cash';
  var TITLE = 'Donate for the Dackel';

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function dialogBody() {
    var box = el('div', 'donate');
    box.appendChild(el('p', 'donate-lead', 'WAMS is free and open source. If it helps you, the Dackel appreciates a few sats.'));
    var section = el('section', 'donate-section');
    section.appendChild(el('h3', null, 'Lightning'));
    var row = el('div', 'donate-row');
    row.appendChild(el('code', 'donate-address', ADDRESS));
    var copy = el('button', 'btn small', 'Copy');
    copy.setAttribute('type', 'button');
    copy.setAttribute('data-donate-copy', '');
    row.appendChild(copy);
    var wallet = el('a', 'btn small donate-wallet', 'Open in wallet');
    wallet.setAttribute('href', 'lightning:' + ADDRESS);
    row.appendChild(wallet);
    section.appendChild(row);
    var status = el('p', 'donate-status');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    section.appendChild(status);
    section.appendChild(el('p', 'hint', 'Works with most Lightning wallets (Lightning address).'));
    box.appendChild(section);
    return box;
  }

  // WAMS usually runs on plain HTTP via a LAN address, where navigator.clipboard is
  // unavailable (no secure context). Fall back to execCommand('copy') on a
  // temporary field, and finally select the address for Ctrl+C.
  function copyAddress(dialog, button) {
    var status = dialog.querySelector('.donate-status');
    function done() { status.textContent = 'Copied'; }
    function manual() {
      try {
        var range = document.createRange();
        range.selectNodeContents(dialog.querySelector('.donate-address'));
        var selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
      } catch (_) { /* selection unavailable */ }
      status.textContent = 'Press Ctrl+C to copy';
    }
    function fallback() {
      var buffer = el('textarea', 'donate-copy-buffer');
      buffer.value = ADDRESS;
      buffer.setAttribute('readonly', '');
      buffer.setAttribute('aria-hidden', 'true');
      buffer.setAttribute('tabindex', '-1');
      dialog.appendChild(buffer);
      var ok = false;
      try {
        buffer.select();
        ok = document.execCommand('copy') === true;
      } catch (_) { ok = false; }
      buffer.remove();
      button.focus();
      if (ok) done(); else manual();
    }
    var clipboard = typeof navigator !== 'undefined' && navigator.clipboard;
    if (window.isSecureContext && clipboard && clipboard.writeText) {
      clipboard.writeText(ADDRESS).then(done, fallback);
    } else fallback();
  }

  function open() {
    window.App.modal(TITLE, dialogBody().outerHTML);
    var dialog = document.getElementById('modal');
    var button = dialog.querySelector('[data-donate-copy]');
    button.addEventListener('click', function () { copyAddress(dialog, button); });
  }

  var trigger = document.getElementById('donate-open');
  if (trigger) trigger.addEventListener('click', open);

  return { open: open, ADDRESS: ADDRESS };
})();
