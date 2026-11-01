// The app pages: chart tooltips, the word lists (search, filters, quiz mode,
// moving words without a page reload), the workout log form and the finance forms.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// --- Chart tooltips: any element with data-tip-v (value) and data-tip-l (label)

const tip = $("#tip");

function showTip(el, x, y) {
  tip.querySelector("b").textContent = el.dataset.tipV;
  tip.querySelector("span").textContent = el.dataset.tipL;
  tip.hidden = false;
  const r = tip.getBoundingClientRect();
  tip.style.left = Math.min(x + 14, innerWidth - r.width - 8) + "px";
  tip.style.top = Math.max(8, y - r.height - 12) + "px";
}

document.addEventListener("pointermove", (e) => {
  const el = e.target.closest?.("[data-tip-v]");
  if (el) showTip(el, e.clientX, e.clientY);
  else tip.hidden = true;
});
document.addEventListener("focusin", (e) => {
  const el = e.target.closest?.("[data-tip-v]");
  if (!el) return;
  const r = el.getBoundingClientRect();
  showTip(el, r.left + r.width / 2, r.top);
});
document.addEventListener("focusout", () => (tip.hidden = true));

// --- Chinese word lists ------------------------------------------------------------

const words = $("#words");

if (words) {
  const q = $("#q"), hsk = $("#f-hsk"), cat = $("#f-cat"), hide = $("#hide");
  const plain = (s) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();

  function visibleCards() {
    return $$(".word", words).filter((c) => !c.hidden);
  }

  function updateShown() {
    const n = visibleCards().length;
    $("#shown").textContent = `${n} word${n === 1 ? "" : "s"}`;
    $("#none").hidden = n > 0;
  }

  function filter() {
    const text = plain(q.value.trim());
    for (const card of $$(".word", words)) {
      card.hidden =
        (text && !plain(card.dataset.text).includes(text)) ||
        (hsk && hsk.value && card.dataset.hsk !== hsk.value) ||
        (cat && cat.value && card.dataset.cat !== cat.value);
    }
    updateShown();
  }
  q.addEventListener("input", filter);
  hsk?.addEventListener("change", filter);
  cat?.addEventListener("change", filter);

  // Quiz mode is remembered per browser
  try {
    hide.checked = localStorage.getItem("hideMeaning") === "1";
  } catch {}
  words.classList.toggle("quiz", hide.checked);
  hide.addEventListener("change", () => {
    words.classList.toggle("quiz", hide.checked);
    try {
      localStorage.setItem("hideMeaning", hide.checked ? "1" : "0");
    } catch {}
  });
  words.addEventListener("click", (e) => {
    const card = e.target.closest(".word");
    if (card && words.classList.contains("quiz") && !e.target.closest("button, a")) card.classList.toggle("shown");
  });

  async function move(ids, to) {
    const body = new URLSearchParams({ ids: ids.join(","), to });
    const res = await fetch("/-/chinese/move", { method: "POST", body, headers: { Accept: "application/json" } });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error);
    for (const [status, n] of Object.entries(data.counts)) {
      const el = $(`[data-count="${status}"]`);
      if (el) el.textContent = n;
    }
    for (const id of ids) {
      const card = $(`.word[data-id="${id}"]`, words);
      card.classList.add("leaving");
      setTimeout(() => {
        card.remove();
        updateShown();
      }, 200);
    }
  }

  // Learnt / Forgot buttons: move the word without reloading the page
  words.addEventListener("submit", (e) => {
    e.preventDefault();
    const form = e.target, button = e.submitter;
    $$("button", form).forEach((b) => (b.disabled = true));
    move([form.elements.namedItem("id").value], button.value).catch(() => form.submit());
  });

  $("#all-learnt")?.addEventListener("click", async () => {
    const ids = visibleCards().map((c) => c.dataset.id);
    if (!ids.length || !confirm(`Mark all ${ids.length} shown word${ids.length === 1 ? "" : "s"} as learnt?`)) return;
    await move(ids, "learnt");
  });
}

// --- Workout log ---------------------------------------------------------------------

const exercise = $("#exercise");

if (exercise) {
  const last = JSON.parse($("#last-sets").textContent);
  // Picking a known exercise fills in what you did last time
  exercise.addEventListener("change", () => {
    const found = last.find((s) => s.exercise.toLowerCase() === exercise.value.trim().toLowerCase());
    if (!found) return;
    $("#reps").value = found.reps;
    $("#weight").value = found.weight;
  });
  for (const form of $$("form.delete")) {
    form.addEventListener("submit", (e) => {
      if (!confirm("Delete this set?")) e.preventDefault();
    });
  }
  // After logging, keep the cursor ready for the next set
  if (new URLSearchParams(location.search).has("msg")) $("#reps").select();
}

// --- Workout plans -------------------------------------------------------------------

const planRows = $("#plan-rows");

if (planRows) {
  const clear = (row) => $$("input", row).forEach((i) => (i.value = ""));
  const muscles = JSON.parse($("#exercise-muscles").textContent);
  // Picking an exercise you've used before fills in its muscle group
  planRows.addEventListener("change", (e) => {
    if (e.target.name !== "exercise") return;
    const muscle = e.target.closest("tr").querySelector("[name=muscle]");
    const known = muscles[e.target.value.trim().toLowerCase()];
    if (known && !muscle.value) muscle.value = known;
  });
  $("#add-row").addEventListener("click", () => {
    const row = planRows.lastElementChild.cloneNode(true);
    clear(row);
    planRows.append(row);
    $("input", row).focus();
  });
  planRows.addEventListener("click", (e) => {
    const button = e.target.closest("button");
    const row = button?.closest("tr");
    if (!row) return;
    if (button.hasAttribute("data-remove")) {
      if (planRows.children.length > 1) row.remove();
      else clear(row);
    } else if (button.dataset.move === "-1" && row.previousElementSibling) {
      row.previousElementSibling.before(row);
    } else if (button.dataset.move === "1" && row.nextElementSibling) {
      row.nextElementSibling.after(row);
    }
  });
}

for (const form of $$("form.delete-plan")) {
  form.addEventListener("submit", (e) => {
    const name = form.querySelector("button").dataset.name;
    if (!confirm(`Delete the plan "${name}"? It also comes off every day it's planned on.`)) e.preventDefault();
  });
}

// --- Finance -------------------------------------------------------------------------

for (const form of $$("form[data-confirm]")) {
  form.addEventListener("submit", (e) => {
    if (!confirm(form.dataset.confirm)) e.preventDefault();
  });
}

const category = $("#category");

if (category) {
  // The category buttons under the field fill it in; the one matching the field is lit
  const chips = $$("[data-category]");
  const mark = () =>
    chips.forEach((c) => c.setAttribute("aria-pressed", c.dataset.category.toLowerCase() === category.value.trim().toLowerCase()));
  for (const chip of chips) {
    chip.addEventListener("click", () => {
      category.value = chip.dataset.category;
      mark();
      $("#amount").focus();
    });
  }
  category.addEventListener("input", mark);
  mark();
}

// Budget editor: add and remove lines, and keep each column's total up to date
for (const box of $$(".budget-edit")) {
  const body = $("tbody", box);
  const total = $("[data-total]", box);
  const update = () => {
    const sum = $$("input[type=number]", body).reduce((s, i) => s + (parseFloat(i.value) || 0), 0);
    total.textContent = "RM" + sum.toLocaleString("en", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  };
  body.addEventListener("input", update);
  $("[data-add-row]", box).addEventListener("click", () => {
    const row = body.lastElementChild.cloneNode(true);
    $$("input", row).forEach((i) => (i.value = ""));
    body.append(row);
    $("input", row).focus();
  });
  body.addEventListener("click", (e) => {
    const row = e.target.closest("[data-remove]")?.closest("tr");
    if (!row) return;
    if (body.children.length > 1) row.remove();
    else $$("input", row).forEach((i) => (i.value = ""));
    update();
  });
}

// Finance: opening the page fetches the latest prices, IBKR's statement and Wise's (the
// server skips it if it did so in the last 5 minutes); then the Investments tab, which
// has nothing to type into, is swapped for one with the new numbers
const refreshStatus = $("[data-refresh-status]");
if (refreshStatus) {
  (async () => {
    refreshStatus.textContent = "Fetching prices, IBKR and Wise…";
    try {
      const result = await (await fetch("/-/finance/refresh", { method: "POST" })).json();
      const live = $("[data-refresh]");
      if (result.fetched && live) {
        const res = await fetch(live.dataset.refresh, { headers: { Accept: "text/html" } });
        const fresh = document.createElement("div");
        fresh.innerHTML = res.ok ? await res.text() : "";
        const next = fresh.querySelector("[data-refresh]");
        if (next) live.replaceWith(next);
      }
      const trouble = /didn't work|Couldn't|went wrong/.test(result.said || "");
      refreshStatus.textContent = trouble ? result.said : `Up to date, fetched at ${result.at}`;
      refreshStatus.title = result.said || "";
    } catch {
      refreshStatus.textContent = "Couldn't fetch the latest: offline?";
    }
  })();
}

// --- Quotes and Knowledge: search what's shown -------------------------------------------

const notes = $("#notes");

if (notes) {
  const q = $("#note-q"), shown = $("#note-shown");
  const plain = (s) => s.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  q.addEventListener("input", () => {
    const text = plain(q.value.trim());
    let n = 0;
    for (const card of $$(".note-card", notes)) {
      card.hidden = text && !plain(card.dataset.text).includes(text);
      if (!card.hidden) n++;
    }
    shown.textContent = `${n} ${n === 1 ? shown.dataset.one : shown.dataset.many}`;
    $("#note-none").hidden = n > 0;
  });
}
