/* lj-signage: small progressive enhancements (the pages work without JavaScript,
   except the drag-and-drop upload, which falls back to the file picker). */
(function () {
  "use strict";

  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
  }

  // Forms that need a confirmation: <form data-confirm="Tem a certeza?">
  document.addEventListener(
    "submit",
    function (event) {
      const form = event.target;
      const message = form.getAttribute("data-confirm");
      if (message && !window.confirm(message)) {
        event.preventDefault();
        event.stopImmediatePropagation();
        return;
      }
      const button = form.querySelector("button[type=submit]");
      if (button && !form.hasAttribute("hx-post")) {
        button.classList.add("is-busy");
      }
    },
    true
  );

  // Drag-and-drop upload with progress (library).
  function initDropzone(zone) {
    const input = zone.querySelector("input[type=file]");
    const list = document.getElementById(zone.dataset.list);
    const target = document.getElementById(zone.dataset.target);

    zone.addEventListener("click", function (event) {
      if (event.target !== input) input.click();
    });
    zone.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        input.click();
      }
    });
    ["dragenter", "dragover"].forEach(function (type) {
      zone.addEventListener(type, function (event) {
        event.preventDefault();
        zone.classList.add("is-over");
      });
    });
    ["dragleave", "drop"].forEach(function (type) {
      zone.addEventListener(type, function (event) {
        event.preventDefault();
        zone.classList.remove("is-over");
      });
    });
    zone.addEventListener("drop", function (event) {
      Array.from(event.dataTransfer.files).forEach(send);
    });
    input.addEventListener("change", function () {
      Array.from(input.files).forEach(send);
      input.value = "";
    });

    function send(file) {
      const item = document.createElement("li");
      item.className = "upload-item";
      const name = document.createElement("div");
      name.textContent = file.name;
      const progress = document.createElement("div");
      progress.className = "progress";
      const bar = document.createElement("span");
      progress.appendChild(bar);
      const status = document.createElement("div");
      status.className = "muted small";
      status.textContent = "A enviar…";
      item.append(name, progress, status);
      list.prepend(item);

      const data = new FormData();
      data.append("file", file);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", zone.dataset.url);
      xhr.setRequestHeader("X-CSRFToken", csrfToken());
      xhr.upload.addEventListener("progress", function (event) {
        if (!event.lengthComputable) return;
        const percent = Math.round((100 * event.loaded) / event.total);
        bar.style.width = percent + "%";
        status.textContent =
          percent < 100 ? "A enviar… " + percent + "%" : "A guardar no servidor…";
      });
      xhr.addEventListener("load", function () {
        if (xhr.status === 201) {
          item.remove();
          const holder = document.createElement("div");
          holder.innerHTML = xhr.responseText.trim();
          const card = holder.firstElementChild;
          target.prepend(card);
          if (window.htmx) window.htmx.process(card);
          const empty = document.getElementById("library-empty");
          if (empty) empty.remove();
          return;
        }
        item.classList.add("alert-error");
        bar.style.width = "0";
        status.textContent = errorText(xhr);
      });
      xhr.addEventListener("error", function () {
        item.classList.add("alert-error");
        status.textContent = "Falha de rede: o envio não foi concluído.";
      });
      xhr.send(data);
    }
  }

  function errorText(xhr) {
    if (xhr.status === 413) return "Ficheiro demasiado grande.";
    const doc = new DOMParser().parseFromString(xhr.responseText || "", "text/html");
    const message = doc.querySelector("[data-message]");
    return (message && message.textContent.trim()) || "Não foi possível enviar o ficheiro.";
  }

  // Schedule form: show only the field of the chosen target (group or store).
  function initTargetSelector(form) {
    function update() {
      const checked = form.querySelector('input[name="target_type"]:checked');
      const value = checked ? checked.value : "all";
      form.querySelectorAll("[data-target-field]").forEach(function (el) {
        el.hidden = el.dataset.targetField !== value;
      });
    }
    form.querySelectorAll('input[name="target_type"]').forEach(function (radio) {
      radio.addEventListener("change", update);
    });
    update();
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-dropzone]").forEach(initDropzone);
    document.querySelectorAll("[data-target-selector]").forEach(initTargetSelector);
  });
})();
