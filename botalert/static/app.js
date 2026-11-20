/* botalert dashboard — plain JS, no framework or build step (FR-019, US4).
 *
 * Contract (contracts/management-api.md "Dashboard contract"):
 * - load -> if no token in localStorage, show the token prompt
 * - verify the token with GET /api/products + GET /api/config
 *   (401 -> clear the stored token and re-prompt)
 * - render the watch-list table (label, URL, status badge, last checked,
 *   failure count) with add/edit/remove, a cadence form, and "Check now"
 * - inline confirmation/error text for EVERY operation (FR-015 parity
 *   with the Discord interface; the API error `message` is shown verbatim)
 * - re-render on load and after each operation ONLY — no auto-refresh,
 *   no push
 */
(function () {
  "use strict";

  var TOKEN_KEY = "botalert-token";
  var $ = function (id) { return document.getElementById(id); };

  var BADGES = {
    in_stock: "\u{1F7E2} in stock",
    out_of_stock: "\u{1F534} out of stock",
    unknown: "\u{26AA} unknown",
  };

  var config = null; // last GET /api/config response
  var PRODUCTS = []; // last GET /api/products payload
  var busy = false;

  // ---------- token + API ----------

  function getToken() {
    try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
  }

  function saveToken(token) {
    try {
      if (token) { localStorage.setItem(TOKEN_KEY, token); }
      else { localStorage.removeItem(TOKEN_KEY); }
    } catch (e) { /* private mode: token just won't persist */ }
  }

  /** Fetch a /api path with the stored token; on 401 re-prompt (clear first).
   *  Resolves with the parsed JSON (null for 204); rejects with the
   *  contract error `message` (verbatim) for every other non-2xx. */
  function api(path, options) {
    options = options || {};
    var headers = { "X-Botalert-Token": getToken() };
    if (options.json !== undefined) { headers["Content-Type"] = "application/json"; }
    var init = { method: options.method || "GET", headers: headers };
    if (options.json !== undefined) { init.body = JSON.stringify(options.json); }
    return fetch(path, init).then(function (res) {
      if (res.status === 401) {
        saveToken("");
        showTokenView("Your token was rejected. Enter it again.");
        return Promise.reject(new Error("unauthorized"));
      }
      return res.json().catch(function () { return null; }).then(function (data) {
        if (!res.ok) {
          var message =
            data && data.error && typeof data.error.message === "string"
              ? data.error.message
              : "Request failed with status " + res.status + ".";
          return Promise.reject(new Error(message));
        }
        return data;
      });
    });
  }

  // ---------- views ----------

  function showTokenView(message) {
    $("token-view").classList.remove("hidden");
    $("main-view").classList.add("hidden");
    $("token-message").textContent = message || "";
    $("token-input").value = "";
    $("token-input").focus();
  }

  function showMainView() {
    $("token-view").classList.add("hidden");
    $("main-view").classList.remove("hidden");
  }

  /** Inline confirmation/error for every operation (FR-015 parity). */
  function announce(message, isError) {
    var el = $("status-message");
    el.textContent = message;
    el.className = isError ? "error" : "ok";
  }

  function setBusy(value) {
    busy = value;
    var ids = ["token-submit", "cadence-save", "add-submit", "check-now", "edit-save"];
    ids.forEach(function (id) { $(id).disabled = value; });
  }

  // ---------- rendering (load + after each operation ONLY) ----------

  function formatTime(iso) {
    if (!iso) { return "\u2014"; }
    var d = new Date(iso);
    return isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
  }

  function render() {
    if (config) {
      $("cadence-input").value = String(config.cadence_minutes);
      $("cadence-input").min = String(config.min_cadence_minutes);
      $("cadence-input").max = String(config.max_cadence_minutes);
      $("cadence-range").textContent =
        "Allowed: " + config.min_cadence_minutes + " \u2013 " +
        config.max_cadence_minutes + " minutes";
    }

    var tbody = $("products-body");
    tbody.textContent = "";
    var products = PRODUCTS;
    if (!products.length) {
      var empty = document.createElement("tr");
      var cell = document.createElement("td");
      cell.colSpan = 6;
      cell.textContent =
        "Watch list is empty \u2014 add a product above to start monitoring.";
      empty.appendChild(cell);
      tbody.appendChild(empty);
      return;
    }
    products.forEach(function (p) { tbody.appendChild(renderRow(p)); });
  }

  function renderRow(p) {
    var tr = document.createElement("tr");

    var label = document.createElement("td");
    label.textContent = p.label;
    tr.appendChild(label);

    var url = document.createElement("td");
    url.className = "url";
    url.textContent = p.url;
    tr.appendChild(url);

    var status = document.createElement("td");
    var badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = BADGES[p.last_status] || BADGES.unknown;
    status.appendChild(badge);
    tr.appendChild(status);

    var checked = document.createElement("td");
    checked.textContent = formatTime(p.last_checked_at);
    tr.appendChild(checked);

    var failures = document.createElement("td");
    var failuresValue = document.createElement("span");
    failuresValue.className =
      p.consecutive_failures > 0 ? "failures" : "failures ok";
    failuresValue.textContent = String(p.consecutive_failures);
    failures.appendChild(failuresValue);
    tr.appendChild(failures);

    var actions = document.createElement("td");
    actions.className = "actions";

    var edit = document.createElement("button");
    edit.type = "button";
    edit.textContent = "Edit";
    edit.addEventListener("click", function () { openEdit(p); });
    actions.appendChild(edit);

    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "danger";
    remove.textContent = "Remove";
    remove.addEventListener("click", function () { removeProduct(p.url); });
    actions.appendChild(remove);

    tr.appendChild(actions);
    return tr;
  }

  /** Load products + config, then render (the only two fetches that feed
   *  the table; done on load and after each operation). */
  function reload() {
    return Promise.all([
      api("/api/products"),
      api("/api/config"),
    ]).then(function (results) {
      PRODUCTS = results[0].products || [];
      config = results[1];
      render();
    });
  }

  // ---------- operations ----------

  function submitToken(event) {
    event.preventDefault();
    var token = $("token-input").value.trim();
    if (!token) {
      $("token-message").textContent = "Enter the token first.";
      return;
    }
    setBusy(true);
    saveToken(token);
    reload()
      .then(function () {
        showMainView();
        announce("Connected \u2014 watch list loaded.", false);
      })
      .catch(function (err) {
        if (err.message !== "unauthorized") {
          $("token-message").textContent = err.message;
        }
        // 401 already cleared the token and re-showed the prompt.
      })
      .then(function () { setBusy(false); });
  }

  function resetToken() {
    saveToken("");
    showTokenView("Enter your dashboard token to continue.");
  }

  function submitAdd(event) {
    event.preventDefault();
    var body = { url: $("add-url").value.trim() };
    if ($("add-label").value.trim()) { body.label = $("add-label").value.trim(); }
    if ($("add-in-indicator").value.trim()) {
      body.in_stock_indicator = $("add-in-indicator").value.trim();
    }
    if ($("add-out-indicator").value.trim()) {
      body.out_of_stock_indicator = $("add-out-indicator").value.trim();
    }
    setBusy(true);
    api("/api/products", { method: "POST", json: body })
      .then(function () {
        $("add-form").reset();
        return reload();
      })
      .then(function () { announce("Product added.", false); })
      .catch(function (err) { announce(err.message, true); })
      .then(function () { setBusy(false); });
  }

  var editTargetUrl = null;

  function openEdit(product) {
    editTargetUrl = product.url;
    $("edit-url-hint").textContent = product.url;
    $("edit-label").value = product.label || "";
    $("edit-in-indicator").value = product.in_stock_indicator || "";
    $("edit-out-indicator").value = product.out_of_stock_indicator || "";
    $("edit-panel").classList.remove("hidden");
    $("edit-label").focus();
  }

  function closeEdit() {
    editTargetUrl = null;
    $("edit-panel").classList.add("hidden");
  }

  function submitEdit(event) {
    event.preventDefault();
    if (!editTargetUrl) { return; }
    // All three fields are always sent: an empty indicator string CLEARS it.
    var body = {
      label: $("edit-label").value,
      in_stock_indicator: $("edit-in-indicator").value,
      out_of_stock_indicator: $("edit-out-indicator").value,
    };
    setBusy(true);
    api("/api/products?url=" + encodeURIComponent(editTargetUrl), {
      method: "PATCH",
      json: body,
    })
      .then(function () {
        closeEdit();
        return reload();
      })
      .then(function () { announce("Product updated.", false); })
      .catch(function (err) { announce(err.message, true); })
      .then(function () { setBusy(false); });
  }

  function removeProduct(url) {
    setBusy(true);
    api("/api/products?url=" + encodeURIComponent(url), { method: "DELETE" })
      .then(function () { return reload(); })
      .then(function () { announce("Product removed.", false); })
      .catch(function (err) { announce(err.message, true); })
      .then(function () { setBusy(false); });
  }

  function submitCadence(event) {
    event.preventDefault();
    var raw = $("cadence-input").value.trim();
    var minutes = Number(raw);
    if (raw === "" || !isFinite(minutes)) {
      announce("Enter a numeric cadence in minutes.", true);
      return;
    }
    setBusy(true);
    api("/api/config", { method: "PUT", json: { cadence_minutes: minutes } })
      .then(function (updated) {
        config = updated;
        render();
        announce(
          "Cadence set to " + updated.cadence_minutes + " minutes.",
          false
        );
      })
      .catch(function (err) { announce(err.message, true); })
      .then(function () { setBusy(false); });
  }

  function checkNow() {
    setBusy(true);
    announce("Cycle requested \u2014 results appear after the next check.", false);
    api("/api/check-now", { method: "POST" })
      .then(function () { return reload(); }) // re-poll the list per the contract
      .catch(function (err) { announce(err.message, true); })
      .then(function () { setBusy(false); });
  }

  // ---------- wiring ----------

  document.addEventListener("DOMContentLoaded", function () {
    $("token-form").addEventListener("submit", submitToken);
    $("token-reset").addEventListener("click", resetToken);
    $("cadence-form").addEventListener("submit", submitCadence);
    $("add-form").addEventListener("submit", submitAdd);
    $("edit-form").addEventListener("submit", submitEdit);
    $("edit-cancel").addEventListener("click", closeEdit);
    $("check-now").addEventListener("click", checkNow);

    if (getToken()) {
      // Re-verify the stored token; a 401 falls back to the prompt.
      reload()
        .then(function () {
          showMainView();
          announce("Connected \u2014 watch list loaded.", false);
        })
        .catch(function (err) {
          if (err.message !== "unauthorized") {
            showTokenView(err.message);
          }
        });
    } else {
      showTokenView();
    }
  });
})();
