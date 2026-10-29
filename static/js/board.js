// Background updates (#322, #384). Every page that lists cards marks one element
// with data-changes-url (core/_board_attrs.html). While the tab is visible this
// polls core:changes for that page's scope and refreshes only the cards an agent
// (or another tab) changed -- in place, never re-sorted, with their expand/Raw/
// details state carried over, and the scroll position pinned, so nothing moves
// under the reader. An open edit form is never replaced; it gets a warning line
// instead. The poll sends the last ETag, and the ETag only advances once every
// change has been applied, so a card deferred because it was busy is retried on
// the next tick. After a change, `cm:board-updated` fires on document so the
// page's own filter re-applies.
(function () {
  var board = document.querySelector('[data-changes-url]');
  if (!board || !window.fetch) {
    return;
  }
  var INTERVAL = 15000;
  var etag = null;
  var timer = null;
  var running = false;
  var dataset = board.dataset;

  function urlFor(template, pk) {
    return template.split('__PK__').join(pk);
  }
  function fetchCard(template, pk) {
    return fetch(urlFor(template, pk), { headers: { 'HX-Request': 'true' }, cache: 'no-store' })
      .then(function (response) {
        if (!response.ok) {
          throw new Error('card ' + pk + ': ' + response.status);
        }
        return response.text();
      })
      .then(function (html) {
        // An empty body means the record left this page (closed), as for htmx swaps.
        var holder = document.createElement('template');
        holder.innerHTML = html.trim();
        return holder.content.firstElementChild;
      });
  }
  function pkOf(element) {
    return element.id.replace(/^(idea|project)-/, '');
  }
  function flash(element) {
    element.classList.add('bg-updated');
    setTimeout(function () {
      element.classList.remove('bg-updated');
    }, 2500);
  }
  // A card the user is in the middle of using: typing into it, a file picked for
  // upload, its status menu open, or one of its own htmx requests in flight.
  // Replacing it would throw that away, so it waits a tick.
  function busy(card) {
    var active = document.activeElement;
    if (active && card.contains(active) && active.matches('input:not([type=checkbox]):not([type=submit]), textarea, select')) {
      return true;
    }
    if (card.querySelector('details.status-menu[open], .htmx-request') || card.classList.contains('htmx-request')) {
      return true;
    }
    return Array.prototype.some.call(card.querySelectorAll('input[type=file]'), function (input) {
      return input.files && input.files.length;
    });
  }
  // Expand checkboxes, Raw / Show-more toggles (all carry ids), open <details>
  // (matched by class, in order) and the filter's `hidden` move over from the old
  // card, so a collapsed card stays collapsed whatever its new status defaults to.
  function carryState(from, to) {
    from.querySelectorAll('input[type=checkbox][id]').forEach(function (box) {
      var twin = to.querySelector('#' + CSS.escape(box.id));
      if (twin) {
        twin.checked = box.checked;
      }
    });
    var open = {};
    from.querySelectorAll('details').forEach(function (d) {
      (open[d.className] = open[d.className] || []).push(d.open);
    });
    to.querySelectorAll('details').forEach(function (d) {
      var states = open[d.className];
      if (states && states.length) {
        d.open = states.shift();
      }
    });
    to.hidden = from.hidden;
  }
  function warn(editing, text, reloadTemplate) {
    var note = editing.querySelector('.edit-conflict');
    if (note && note.dataset.text === text) {
      return;
    }
    if (!note) {
      note = document.createElement('p');
      note.className = 'edit-conflict';
      note.setAttribute('role', 'status');
      editing.querySelector('form').prepend(note);
    }
    note.dataset.text = text;
    note.textContent = text + ' ';
    var reload = document.createElement('a');
    var url = urlFor(reloadTemplate, pkOf(editing));
    reload.href = url;
    reload.setAttribute('hx-get', url);
    reload.setAttribute('hx-target', '#' + editing.id);
    reload.setAttribute('hx-swap', 'outerHTML');
    reload.textContent = 'Reload the form';
    note.appendChild(reload);
    htmx.process(note);
  }
  // The first card or project header still on screen, re-found by id after the
  // swaps (its own node may have been replaced) to scroll back to where it was.
  function pinViewport() {
    var candidates = board.querySelectorAll('[id^="project-"][data-sig], li.idea[id^="idea-"]');
    for (var i = 0; i < candidates.length; i++) {
      var rect = candidates[i].getBoundingClientRect();
      if (rect.height && rect.bottom > 0) {
        return { id: candidates[i].id, top: rect.top };
      }
    }
    return null;
  }
  function restoreViewport(pin) {
    var element = pin && document.getElementById(pin.id);
    if (element) {
      window.scrollBy(0, element.getBoundingClientRect().top - pin.top);
    }
  }

  function statusLabel(status) {
    return status.charAt(0).toUpperCase() + status.slice(1);
  }

  // Where a card new to this page goes: the top of its status section on the
  // Ideas view, right below the add slot on a project page -- where the sort
  // puts it. Nowhere on a page that cannot gain cards (an idea's own page).
  function insertionPoint(row) {
    if (dataset.insert === 'status') {
      var list = document.getElementById('ideas-' + row[2]);
      return list && { insert: function (fresh) { list.prepend(fresh); } };
    }
    if (dataset.insert === 'slot') {
      var slot = document.getElementById('new-idea-' + row[1]);
      return slot && { insert: function (fresh) { slot.after(fresh); } };
    }
    return null;
  }

  function apply(data) {
    var wanted = {};
    data.ideas.forEach(function (row) {
      wanted[row[0]] = { status: row[2], version: row[3] };
    });
    var jobs = [];
    var complete = true;
    var onPage = {};

    // Ideas on the page: changed, or gone from this page's scope (closed, moved
    // to another project, deleted).
    board.querySelectorAll('li.idea[id^="idea-"]').forEach(function (card) {
      var pk = pkOf(card);
      var want = wanted[pk];
      if (want) {
        onPage[pk] = true;
      }
      if (want && card.dataset.version === want.version) {
        return;
      }
      if (card.classList.contains('idea-editing')) {
        warn(card, want
          ? 'Changed elsewhere since you opened this form (now ' + statusLabel(want.status) + '). Saving overwrites that change.'
          : 'This idea was closed, moved or deleted elsewhere since you opened this form.',
          dataset.ideaEditUrl);
        return;
      }
      if (busy(card)) {
        complete = false;
        return;
      }
      if (!want) {
        jobs.push(Promise.resolve(function () { card.remove(); }));
        return;
      }
      jobs.push(fetchCard(dataset.ideaCardUrl, pk).then(function (fresh) {
        return function () {
          if (!card.isConnected) {
            return;
          }
          if (!fresh) {
            card.remove();
            return;
          }
          carryState(card, fresh);
          card.replaceWith(fresh);
          htmx.process(fresh);
          flash(fresh);
        };
      }));
    });

    // Ideas new to the page (created, reopened, moved in). Newest last, so that
    // after prepending one by one the newest ends up on top.
    data.ideas.slice().reverse().forEach(function (row) {
      var pk = String(row[0]);
      if (onPage[pk] || document.getElementById('idea-' + pk)) {
        return;
      }
      var point = insertionPoint(row);
      if (!point) {
        return;
      }
      jobs.push(fetchCard(dataset.ideaCardUrl, pk).then(function (fresh) {
        return function () {
          if (fresh && !document.getElementById(fresh.id)) {
            point.insert(fresh);
            htmx.process(fresh);
            flash(fresh);
          }
        };
      }));
    });

    // Project rows (Projects view) and a project page's header: replaced whole.
    board.querySelectorAll('[id^="project-"][data-sig]').forEach(function (project) {
      var pk = pkOf(project);
      var sig = data.projects[pk];
      if (sig === project.dataset.sig) {
        return;
      }
      if (project.classList.contains('project-editing')) {
        warn(project, sig
          ? 'Changed elsewhere since you opened this form. Saving overwrites that change.'
          : 'This project was closed elsewhere since you opened this form.',
          dataset.projectEditUrl);
        return;
      }
      if (busy(project)) {
        complete = false;
        return;
      }
      if (sig === undefined) {
        jobs.push(Promise.resolve(function () { project.remove(); }));
        return;
      }
      jobs.push(fetchCard(dataset.projectCardUrl, pk).then(function (fresh) {
        return function () {
          if (!project.isConnected) {
            return;
          }
          if (!fresh) {
            project.remove();
            return;
          }
          fresh.hidden = project.hidden;
          project.replaceWith(fresh);
          htmx.process(fresh);
          flash(fresh);
        };
      }));
    });

    if (!jobs.length) {
      return Promise.resolve(complete);
    }
    // Fetch everything first and then change the DOM in one go, so the page
    // moves (and is pinned back) once rather than once per card.
    return Promise.all(jobs).then(function (changes) {
      var pin = pinViewport();
      changes.forEach(function (change) {
        change();
      });
      restoreViewport(pin);
      document.dispatchEvent(new CustomEvent('cm:board-updated'));
      return complete;
    });
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(poll, INTERVAL);
  }
  function poll() {
    if (running || document.visibilityState !== 'visible') {
      // Hidden: no timer at all; becoming visible again polls straight away.
      return;
    }
    running = true;
    var headers = etag ? { 'If-None-Match': etag } : {};
    var nextEtag = null;
    fetch(dataset.changesUrl, { headers: headers, cache: 'no-store' })
      .then(function (response) {
        if (response.status === 304) {
          return null;
        }
        if (!response.ok) {
          throw new Error('changes: ' + response.status);
        }
        nextEtag = response.headers.get('ETag');
        return response.json();
      })
      .then(function (data) {
        return data && apply(data);
      })
      .then(function (complete) {
        if (complete && nextEtag) {
          etag = nextEtag;
        }
      })
      .catch(function () {
        // Offline or a server hiccup: say nothing, the next tick tries again.
      })
      .then(function () {
        running = false;
        schedule();
      });
  }
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') {
      poll();
    } else {
      clearTimeout(timer);
    }
  });
  window.addEventListener('online', poll);
  schedule();
})();
