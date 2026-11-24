(() => {
  const S = window.SITE || {};
  const reduzir = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let celular = matchMedia("(max-width: 760px)").matches;

  /* ── Rolagem que troca as imagens do hero ─────────────────────────────── */
  const cine = document.querySelector(".cine");
  const imagem = cine && cine.querySelector(".cine-imagem");
  if (cine && imagem) {
    cine.style.setProperty("--altura", "180vh");

    const momentos = [...cine.querySelectorAll(".momento")];
    let agendado = false;
    let quadros = [];
    let indiceAtual = -1;

    const progresso = () => {
      const r = cine.getBoundingClientRect();
      const curso = cine.offsetHeight - innerHeight;
      return Math.min(1, Math.max(0, -r.top / (curso || 1)));
    };

    const atualizarMomentos = (p) => {
      for (const m of momentos) {
        const de = parseFloat(m.dataset.de || 0), ate = parseFloat(m.dataset.ate || 1);
        m.classList.toggle("ativo", p >= de && (p < ate || (ate >= 1 && p >= 1)));
      }
      cine.classList.toggle("andou", p > 0.02);
    };

    const carregarSequencia = () => {
      const cfg = S.frames || {};
      const modelo = celular ? cfg.mobile : cfg.desktop;
      const fontes = Array.isArray(modelo)
        ? modelo.filter(Boolean)
        : Array.from({ length: Number(cfg.total) || 6 }, (_, i) =>
            String(modelo || "").replace("{n}", String(i + 1).padStart(2, "0")),
          );
      quadros = fontes.map((src) => {
        const quadro = new Image();
        quadro.decoding = "async";
        quadro.src = src;
        return quadro;
      });
      indiceAtual = -1;
      if (quadros.length) imagem.src = quadros[0].src;
    };

    const aplicar = () => {
      const p = progresso();
      atualizarMomentos(p);
      if (!quadros.length) return;
      const indice = Math.min(quadros.length - 1, Math.round(p * (quadros.length - 1)));
      if (indice === indiceAtual) return;
      indiceAtual = indice;
      const quadro = quadros[indice];
      const trocar = () => { if (indiceAtual === indice) imagem.src = quadro.src; };
      if (quadro.complete && quadro.naturalWidth) trocar();
      else if (quadro.decode) quadro.decode().then(trocar, trocar);
      else quadro.onload = trocar;
    };

    const aoRolar = () => {
      if (agendado) return;
      agendado = true;
      requestAnimationFrame(() => { agendado = false; aplicar(); });
    };

    const aoRedimensionar = () => {
      const agoraCelular = matchMedia("(max-width: 760px)").matches;
      if (agoraCelular !== celular) {
        celular = agoraCelular;
        cine.style.setProperty("--altura", "180vh");
        carregarSequencia();
      }
      aoRolar();
    };

    carregarSequencia();
    addEventListener("scroll", aoRolar, { passive: true });
    addEventListener("resize", aoRedimensionar, { passive: true });
    aplicar();
    if (reduzir) atualizarMomentos(0);
  }

  /* ── Parallax leve nas artes ──────────────────────────────────────────── */
  const artes = reduzir ? [] : [...document.querySelectorAll(".arte")];
  if (artes.length) {
    const mover = () => {
      for (const a of artes) {
        const r = a.parentElement.getBoundingClientRect();
        if (r.bottom < 0 || r.top > innerHeight) continue;
        const c = (r.top + r.height / 2 - innerHeight / 2) / innerHeight;
        a.style.transform = `translate3d(0, ${(c * -60).toFixed(1)}px, 0) scale(1.08)`;
      }
    };
    addEventListener("scroll", () => requestAnimationFrame(mover), { passive: true });
    mover();
  }

  /* ── Topo, revelar e ano ──────────────────────────────────────────────── */
  const topo = document.querySelector(".topo");
  const whats = document.querySelector(".whats");
  let formNaTela = false; // o botão flutuante se recolhe para não cobrir o "Enviar"
  const aoRolar = () => {
    topo && topo.classList.toggle("rolou", scrollY > 40);
    whats && whats.classList.toggle("visivel", scrollY > innerHeight * 0.6 && !formNaTela);
  };
  const formEl = document.querySelector(".form");
  if (formEl) new IntersectionObserver(([e]) => { formNaTela = e.isIntersecting; aoRolar(); }).observe(formEl);
  addEventListener("scroll", aoRolar, { passive: true });
  aoRolar();

  const io = new IntersectionObserver((es) => es.forEach((e) => {
    if (e.isIntersecting) { e.target.classList.add("visto"); io.unobserve(e.target); }
  }), { threshold: 0.18 });
  document.querySelectorAll(".revelar").forEach((el) => io.observe(el));
  document.querySelectorAll("[data-ano]").forEach((el) => (el.textContent = new Date().getFullYear()));

  /* ── WhatsApp ─────────────────────────────────────────────────────────── */
  if (whats && S.whatsapp) {
    const num = String(S.whatsapp.numero || "").replace(/\D/g, "");
    whats.href = `https://wa.me/${num}?text=${encodeURIComponent(S.whatsapp.mensagem || "")}`;
  }

  /* ── Formulário → e-mail ──────────────────────────────────────────────── */
  const form = document.querySelector(".form");
  if (form && S.contato) {
    const status = form.querySelector(".form-status");
    const botao = form.querySelector("button[type=submit]");
    const avisar = (txt, tipo) => { status.textContent = txt; status.className = `form-status campo--inteiro ${tipo || ""}`; };

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (form.elements._honey.value) return; // robô
      let valido = true;
      form.querySelectorAll("[required]").forEach((c) => {
        const ok = c.type === "email" ? /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(c.value) : c.value.trim().length > 1;
        c.closest(".campo").classList.toggle("erro", !ok);
        if (!ok) valido = false;
      });
      if (!valido) return avisar("Confira os campos destacados.", "falha");

      const dados = Object.fromEntries(new FormData(form));
      delete dados._honey;
      const assunto = S.contato.assunto || `Novo contato pelo site ${S.nome || ""}`;
      let url, corpo;
      if (S.contato.web3formsChave) {
        url = "https://api.web3forms.com/submit";
        corpo = { access_key: S.contato.web3formsChave, subject: assunto, from_name: S.nome, ...dados };
      } else {
        url = `https://formsubmit.co/ajax/${encodeURIComponent(S.contato.email)}`;
        corpo = { _subject: assunto, _template: "table", _captcha: "false", ...dados };
      }

      botao.disabled = true;
      avisar("Enviando…");
      try {
        const r = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify(corpo),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok || j.success === false || j.success === "false") throw new Error(j.message || r.status);
        form.reset();
        avisar("Recebemos seu contato. Em breve falamos com você.", "ok");
      } catch (e) {
        console.warn("Falha no envio do formulário:", e);
        avisar("Não foi possível enviar agora. Chame a gente no WhatsApp.", "falha");
      } finally {
        botao.disabled = false;
      }
    });
  }
})();
