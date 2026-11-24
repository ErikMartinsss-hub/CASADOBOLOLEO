(function () {
  "use strict";

  var mobile = window.matchMedia("(max-width: 900px)");

  document.querySelectorAll("[data-nav-toggle]").forEach(function (button) {
    var nav = document.getElementById(button.getAttribute("aria-controls"));
    var header = button.closest("header");
    if (!nav || !header) return;

    var ultimoFoco = null;

    function focusaveis() {
      return [button].concat(Array.prototype.slice.call(nav.querySelectorAll("a[href], button:not([disabled])")));
    }

    function abrir() {
      if (button.getAttribute("aria-expanded") === "true") return;
      ultimoFoco = document.activeElement;
      button.setAttribute("aria-expanded", "true");
      button.setAttribute("aria-label", "Fechar menu");
      nav.classList.add("is-open");
      document.body.classList.add("nav-aberta");
      if (mobile.matches) nav.inert = false;
      var primeiro = nav.querySelector("a[href]");
      if (primeiro && mobile.matches) {
        requestAnimationFrame(function () {
          if (button.getAttribute("aria-expanded") === "true") primeiro.focus();
        });
      }
    }

    function fechar(devolverFoco) {
      if (button.getAttribute("aria-expanded") !== "true") return;
      button.setAttribute("aria-expanded", "false");
      button.setAttribute("aria-label", "Abrir menu");
      nav.classList.remove("is-open");
      document.body.classList.remove("nav-aberta");
      if (mobile.matches) nav.inert = true;
      if (devolverFoco && ultimoFoco && mobile.matches && document.contains(ultimoFoco)) ultimoFoco.focus();
    }

    button.addEventListener("click", function () {
      if (button.getAttribute("aria-expanded") === "true") fechar(true);
      else abrir();
    });

    nav.addEventListener("click", function (event) {
      if (event.target.closest("a")) fechar(false);
    });

    document.addEventListener("click", function (event) {
      if (!header.contains(event.target) && !nav.contains(event.target)) fechar(false);
    });

    document.addEventListener("keydown", function (event) {
      if (button.getAttribute("aria-expanded") !== "true") return;
      if (event.key === "Escape") {
        fechar(true);
        return;
      }
      if (event.key !== "Tab" || !mobile.matches) return;
      var itens = focusaveis().filter(function (el) { return el.offsetParent !== null; });
      if (!itens.length) return;
      var primeiro = itens[0];
      var ultimo = itens[itens.length - 1];
      if (event.shiftKey && document.activeElement === primeiro) {
        event.preventDefault();
        ultimo.focus();
      } else if (!event.shiftKey && document.activeElement === ultimo) {
        event.preventDefault();
        primeiro.focus();
      }
    });

    mobile.addEventListener("change", function () {
      if (!mobile.matches) fechar(false);
      else nav.inert = button.getAttribute("aria-expanded") !== "true";
    });

    if (mobile.matches) nav.inert = true;
  });
})();
