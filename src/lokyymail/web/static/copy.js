// Kopier-Knöpfe: <button data-copy="ID"> kopiert den Text des Elements mit dieser ID.
document.addEventListener("click", function (event) {
  var button = event.target.closest("[data-copy]");
  if (!button) return;
  var source = document.getElementById(button.getAttribute("data-copy"));
  if (!source) return;
  var text = source.textContent;
  var done = function () {
    var old = button.getAttribute("data-label") || button.textContent;
    button.setAttribute("data-label", old);
    button.textContent = "Kopiert ✓";
    setTimeout(function () { button.textContent = old; }, 1800);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done, function () { selectText(source); });
  } else {
    selectText(source);
  }
  function selectText(node) {
    var range = document.createRange();
    range.selectNodeContents(node);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    try { if (document.execCommand("copy")) done(); } catch (e) { /* Text bleibt markiert */ }
  }
});
