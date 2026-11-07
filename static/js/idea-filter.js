// Client-side filter box, shared by the Ideas page (cards grouped in status
// sections) and a project page (one flat list of cards). Cards are only hidden,
// never removed, so an open edit form survives a query it no longer matches.
//
// Each card's data-filter is the text to match. A card that also carries
// data-filter-id (project page) is matched by exact id when the query is only
// digits, optionally with a leading "#" -- "147" must not match every goal that
// contains 147. core/views.py:_idea_matches applies the same rule for JS-off.
(function () {
  var input = document.querySelector('.project-filter input[name=q]');
  if (!input) {
    return;
  }

  function cardMatches(card, raw, q) {
    // An open edit form has no data-filter; keep it visible rather than hide
    // what the user is typing into.
    var text = card.dataset.filter;
    if (!q || text === undefined) {
      return true;
    }
    var id = card.dataset.filterId;
    if (id !== undefined && /^#?\d+$/.test(raw)) {
      return raw.replace('#', '') === id;
    }
    return text.toLowerCase().indexOf(q) !== -1;
  }

  function applyFilter() {
    var raw = input.value.trim();
    var q = raw.toLowerCase();
    var anyVisible = false;
    var sections = document.querySelectorAll('#idea-board .idea-section');
    if (sections.length) {
      // Ideas page: a section with no visible card is hidden with it, and each
      // heading counts what it shows.
      sections.forEach(function (section) {
        var shown = 0;
        var cards = section.querySelectorAll('li.idea[id^="idea-"]');
        cards.forEach(function (card) {
          var matches = cardMatches(card, raw, q);
          card.hidden = !matches;
          if (matches) {
            shown += 1;
          }
        });
        section.hidden = cards.length > 0 && shown === 0;
        section.querySelector('.idea-section-count').textContent = q ? shown + ' of ' + cards.length : cards.length;
        anyVisible = anyVisible || shown > 0;
      });
    } else {
      // Project page: one list, one count next to the "Ideas" heading.
      var list = document.querySelectorAll('.project-ideas li.idea[id^="idea-"]');
      var shownCards = 0;
      list.forEach(function (card) {
        var matches = cardMatches(card, raw, q);
        card.hidden = !matches;
        if (matches) {
          shownCards += 1;
        }
      });
      anyVisible = shownCards > 0;
      var count = document.querySelector('.idea-filter-count');
      if (count) {
        count.textContent = q && list.length ? shownCards + ' of ' + list.length : '';
      }
    }
    var noMatch = document.querySelector('.no-match-message');
    if (noMatch) {
      noMatch.hidden = anyVisible || !document.querySelector('li.idea[id^="idea-"]');
      noMatch.querySelector('.no-match-query').textContent = raw;
    }
    // Links that reload the page keep the query.
    document.querySelectorAll('.closed-toggle').forEach(function (link) {
      var url = new URL(link.getAttribute('href'), window.location.href);
      if (raw) {
        url.searchParams.set('q', raw);
      } else {
        url.searchParams.delete('q');
      }
      link.setAttribute('href', url.pathname + url.search);
    });
    var params = new URLSearchParams(window.location.search);
    if (raw) {
      params.set('q', raw);
    } else {
      params.delete('q');
    }
    var query = params.toString();
    history.replaceState(null, '', window.location.pathname + (query ? '?' + query : ''));
  }

  input.addEventListener('input', applyFilter);
  // A JS-off ?q= load renders the right cards hidden; this also counts "n of m".
  applyFilter();
  document.addEventListener('click', function (event) {
    if (event.target.closest('.filter-clear')) {
      event.preventDefault();
      input.value = '';
      applyFilter();
      input.focus();
    }
  });
  // A card swapped in (transition, edit, upload) does not know the query, and a
  // closed one leaves its section: re-apply, which also re-counts the headings.
  // The background poll fires the same event after it changed the board.
  document.body.addEventListener('htmx:afterSwap', applyFilter);
  document.addEventListener('cm:board-updated', applyFilter);
})();
