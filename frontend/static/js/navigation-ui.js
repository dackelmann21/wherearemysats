// Disclosure-menu presentation only. App retains all routing and page behavior.
(function () {
  var nav = document.querySelector('.tabs');
  if (!nav) return;
  var groups = Array.from(nav.querySelectorAll('.nav-group'));
  groups.forEach(function (group) {
    group.addEventListener('toggle', function () {
      if (group.open) groups.forEach(function (other) { if (other !== group) other.open = false; });
    });
  });
  document.addEventListener('click', function (event) {
    groups.forEach(function (group) {
      if (group.contains(event.target) && event.target.closest('.tab')) {
        group.open = false;
        group.querySelector('summary').focus();
      } else if (!group.contains(event.target)) group.open = false;
    });
  });
  nav.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') return;
    var group = event.target.closest('.nav-group');
    if (group && group.open) {
      group.open = false;
      group.querySelector('summary').focus();
    }
  });
})();
