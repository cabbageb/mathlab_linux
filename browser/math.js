(function exposeMathMarkdown(root, factory) {
  const api = factory();
  root.MathMarkdown = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, () => {
  const displayEnvironments = new Set([
    'equation', 'equation*',
    'align', 'align*',
    'gather', 'gather*',
    'multline', 'multline*',
  ]);

  function isEscaped(source, index) {
    let backslashes = 0;
    for (let cursor = index - 1; cursor >= 0 && source[cursor] === '\\'; cursor -= 1) {
      backslashes += 1;
    }
    return backslashes % 2 === 1;
  }

  function findUnescaped(source, token, start, stopAtNewline = false) {
    let cursor = start;
    while (cursor < source.length) {
      const found = source.indexOf(token, cursor);
      if (found < 0) return -1;
      if (stopAtNewline && source.slice(cursor, found).includes('\n')) return -1;
      if (!isEscaped(source, found)) return found;
      cursor = found + token.length;
    }
    return -1;
  }

  function findSingleDollar(source, start) {
    let cursor = start;
    while (cursor < source.length && source[cursor] !== '\n') {
      if (
        source[cursor] === '$'
        && source[cursor - 1] !== '$'
        && source[cursor + 1] !== '$'
        && !isEscaped(source, cursor)
      ) return cursor;
      cursor += 1;
    }
    return -1;
  }

  function codeSpanEnd(source, index) {
    let width = 1;
    while (source[index + width] === '`') width += 1;
    const marker = '`'.repeat(width);
    let cursor = index + width;
    while (cursor < source.length) {
      const found = source.indexOf(marker, cursor);
      if (found < 0) return source.length;
      if (source[found - 1] !== '`' && source[found + width] !== '`') {
        return found + width;
      }
      cursor = found + width;
    }
    return source.length;
  }

  function fencedCodeEnd(source, index) {
    if (index > 0 && source[index - 1] !== '\n') return null;
    const opening = source.slice(index).match(/^ {0,3}(`{3,}|~{3,})[^\n]*(?:\n|$)/);
    if (!opening) return null;
    const marker = opening[1][0];
    const width = opening[1].length;
    let cursor = index + opening[0].length;
    while (cursor < source.length) {
      const nextNewline = source.indexOf('\n', cursor);
      const lineEnd = nextNewline < 0 ? source.length : nextNewline;
      const line = source.slice(cursor, lineEnd);
      const body = line.match(/^ {0,3}(.*)$/)?.[1] || '';
      let count = 0;
      while (body[count] === marker) count += 1;
      if (count >= width && body.slice(count).trim() === '') {
        return nextNewline < 0 ? source.length : nextNewline + 1;
      }
      cursor = nextNewline < 0 ? source.length : nextNewline + 1;
    }
    return source.length;
  }

  function environmentAt(source, index) {
    if (isEscaped(source, index)) return null;
    const match = source.slice(index).match(/^\\begin\{([A-Za-z]+\*?)\}/);
    if (!match || !displayEnvironments.has(match[1])) return null;
    return {name: match[1], openingLength: match[0].length};
  }

  let renderNonce = 0;

  function placeholder(formulas, nonce, display, source, environment = false) {
    const id = `math-${formulas.length}`;
    const token = `MATHLAB_${nonce}_${id}_${display ? 'D' : 'I'}`;
    formulas.push({id, token, display, source, environment});
    return token;
  }

  function appendPlaceholder(
    markdown,
    formulas,
    nonce,
    display,
    formulaSource,
    source,
    openingIndex,
    closingEnd,
    environment = false,
  ) {
    const token = placeholder(formulas, nonce, display, formulaSource, environment);
    if (!display) return markdown + token;

    const lineStart = source.lastIndexOf('\n', openingIndex - 1) + 1;
    const nextNewline = source.indexOf('\n', closingEnd);
    const lineEnd = nextNewline < 0 ? source.length : nextNewline;
    const prefix = source.slice(lineStart, openingIndex);
    const suffix = source.slice(closingEnd, lineEnd);

    // A display formula on its own indented line is commonly a continuation
    // of a Markdown list item.  Preserve that indentation on the private token;
    // otherwise Marked closes the list and parses the following text as code.
    if (/^[ \t]*$/.test(prefix) && /^[ \t]*$/.test(suffix)) {
      return `${markdown.slice(0, markdown.length - prefix.length)}\n${prefix}${token}\n`;
    }
    return `${markdown}\n\n${token}\n\n`;
  }

  function protectMathForMarkdown(value) {
    const source = String(value || '');
    const formulas = [];
    const nonce = `${Date.now().toString(36)}_${(renderNonce += 1).toString(36)}`;
    let markdown = '';
    let index = 0;
    let unclosedIndex = null;

    while (index < source.length) {
      const fenceEnd = fencedCodeEnd(source, index);
      if (fenceEnd !== null) {
        markdown += source.slice(index, fenceEnd);
        index = fenceEnd;
        continue;
      }

      if (source[index] === '`') {
        const closing = codeSpanEnd(source, index);
        markdown += source.slice(index, closing);
        index = closing;
        continue;
      }

      const environment = source.startsWith('\\begin{', index)
        ? environmentAt(source, index)
        : null;
      if (environment) {
        const closingToken = `\\end{${environment.name}}`;
        const closing = findUnescaped(
          source,
          closingToken,
          index + environment.openingLength,
        );
        if (closing < 0) {
          unclosedIndex = index;
          markdown += source.slice(index);
          break;
        }
        const end = closing + closingToken.length;
        markdown = appendPlaceholder(
          markdown, formulas, nonce, true, source.slice(index, end),
          source, index, end, true,
        );
        index = end;
        continue;
      }

      if (source.startsWith('$$', index) && !isEscaped(source, index)) {
        const closing = findUnescaped(source, '$$', index + 2);
        if (closing < 0) {
          unclosedIndex = index;
          markdown += source.slice(index);
          break;
        }
        const end = closing + 2;
        markdown = appendPlaceholder(
          markdown, formulas, nonce, true, source.slice(index + 2, closing).trim(),
          source, index, end,
        );
        index = end;
        continue;
      }

      if (source.startsWith('\\[', index) && !isEscaped(source, index)) {
        const closing = findUnescaped(source, '\\]', index + 2);
        if (closing < 0) {
          unclosedIndex = index;
          markdown += source.slice(index);
          break;
        }
        const end = closing + 2;
        markdown = appendPlaceholder(
          markdown, formulas, nonce, true, source.slice(index + 2, closing).trim(),
          source, index, end,
        );
        index = end;
        continue;
      }

      if (source.startsWith('\\(', index) && !isEscaped(source, index)) {
        const closing = findUnescaped(source, '\\)', index + 2, true);
        if (closing < 0) {
          unclosedIndex = index;
          markdown += source.slice(index);
          break;
        }
        markdown = appendPlaceholder(
          markdown, formulas, nonce, false, source.slice(index + 2, closing),
          source, index, closing + 2,
        );
        index = closing + 2;
        continue;
      }

      if (
        source[index] === '$'
        && source[index - 1] !== '$'
        && source[index + 1] !== '$'
        && !isEscaped(source, index)
      ) {
        const closing = findSingleDollar(source, index + 1);
        if (closing < 0) {
          unclosedIndex = index;
          markdown += source.slice(index);
          break;
        }
        markdown = appendPlaceholder(
          markdown, formulas, nonce, false, source.slice(index + 1, closing),
          source, index, closing + 1,
        );
        index = closing + 1;
        continue;
      }

      markdown += source[index];
      index += 1;
    }

    return {markdown, formulas, unclosedIndex};
  }

  function safeMarkdownPrefix(source) {
    const value = String(source || '');
    const result = protectMathForMarkdown(value);
    return result.unclosedIndex === null
      ? value
      : value.slice(0, result.unclosedIndex);
  }

  return {protectMathForMarkdown, safeMarkdownPrefix};
}));

(function exposeMathLabRenderer(root) {
  'use strict';
  const helpers = root.MathMarkdown || {};
  let renderChain = Promise.resolve();
  let startupEngine = null;
  let startupReady = null;
  let startupFailure = null;
  const typesetFailures = new Map();

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
  }

  function markedParse(source) {
    if (!root.marked) return `<pre>${escapeHtml(source)}</pre>`;
    const options = {gfm: true, breaks: false};
    // MathLab accepts Markdown, not arbitrary user HTML. Formula placeholders
    // are private text sentinels, so every raw HTML token can be rendered as text.
    if (typeof root.marked.Renderer === 'function') {
      const renderer = new root.marked.Renderer();
      renderer.html = raw => {
        const value = String(raw?.raw ?? raw ?? '').trim();
        return escapeHtml(value);
      };
      options.renderer = renderer;
    }
    if (typeof root.marked.parse === 'function') return root.marked.parse(source, options);
    if (typeof root.marked === 'function') return root.marked(source, options);
    return `<pre>${escapeHtml(source)}</pre>`;
  }

  function sanitize(html) {
    return root.DOMPurify?.sanitize
      ? root.DOMPurify.sanitize(html, {USE_PROFILES: {html: true}, FORBID_TAGS: ['style'], FORBID_ATTR: ['style']})
      : escapeHtml(html);
  }

  function formulaText(formula) {
    if (formula.environment) return formula.source;
    return formula.display ? `\\[${formula.source}\\]` : `\\(${formula.source}\\)`;
  }

  function restoreMath(container, formulas) {
    const byToken = new Map((formulas || []).map(formula => [formula.token, formula]));
    if (!byToken.size) return;
    const pattern = new RegExp([...byToken.keys()].map(token => token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'g');
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    const textNodes = [];
    while (walker.nextNode()) textNodes.push(walker.currentNode);
    for (const textNode of textNodes) {
      if (![...byToken.keys()].some(token => textNode.data.includes(token))) continue;
      const exact = byToken.get(textNode.data.trim());
      if (exact?.display && textNode.parentElement?.tagName === 'P' && textNode.parentElement.textContent.trim() === exact.token) {
        const block = document.createElement('div');
        block.className = 'math-display';
        block.textContent = formulaText(exact);
        textNode.parentElement.replaceWith(block);
        continue;
      }
      const fragment = document.createDocumentFragment();
      let cursor = 0;
      for (const match of textNode.data.matchAll(pattern)) {
        if (match.index > cursor) fragment.append(document.createTextNode(textNode.data.slice(cursor, match.index)));
        const formula = byToken.get(match[0]);
        const node = document.createElement(formula.display ? 'div' : 'span');
        node.className = formula.display ? 'math-display' : 'math-inline';
        node.textContent = formulaText(formula);
        fragment.append(node);
        cursor = match.index + match[0].length;
      }
      if (cursor < textNode.data.length) fragment.append(document.createTextNode(textNode.data.slice(cursor)));
      textNode.replaceWith(fragment);
    }
  }

  async function waitForMathJax() {
    try {
      if (!root.MathJax) throw new Error('MathJax 未加载');
      if (startupEngine !== root.MathJax || !startupReady) {
        startupEngine = root.MathJax;
        startupReady = Promise.resolve(root.MathJax.startup?.promise);
      }
      await startupReady;
      if (typeof root.MathJax.typesetPromise !== 'function') throw new Error('MathJax typesetPromise 不可用');
      startupFailure = null;
    } catch (error) {
      startupFailure = error;
      throw error;
    }
  }

  async function typeset(container) {
    return typesetMany([container]);
  }

  async function typesetMany(containers) {
    const active = [...new Set(containers)].filter(container => container?.isConnected);
    if (!active.length) return;
    try {
      await waitForMathJax();
      if (typeof root.MathJax.typesetClear === 'function') root.MathJax.typesetClear(active);
      await root.MathJax.typesetPromise(active);
      for (const container of active) {
        // MathJax may render an inline error instead of rejecting its promise.
        const errorNode = container.querySelector('[data-mjx-error], mjx-merror');
        if (errorNode) typesetFailures.set(container, new Error(errorNode.getAttribute('data-mjx-error') || errorNode.textContent));
        else typesetFailures.delete(container);
      }
      showRendererError();
    } catch (error) {
      if (!startupFailure) for (const container of active) typesetFailures.set(container, error);
      showRendererError();
      throw error;
    }
  }

  function enqueue(task) {
    renderChain = renderChain.then(task, task);
    return renderChain;
  }

  function showRendererError() {
    for (const container of typesetFailures.keys()) {
      if (!container.isConnected) typesetFailures.delete(container);
    }
    const banner = document.getElementById('math-render-error');
    if (banner) {
      const error = startupFailure || typesetFailures.values().next().value;
      const english = document.documentElement.lang === 'en';
      const label = startupFailure
        ? (english ? 'Math renderer initialization failed' : '数学渲染引擎未能初始化')
        : (english ? 'Math typesetting failed' : '本次数学公式渲染失败');
      banner.textContent = error ? `${label}: ${error.message || error}` : '';
      banner.classList.toggle('hidden', !error);
      if (error) banner.dataset.errorPhase = startupFailure ? 'startup' : 'typeset';
      else delete banner.dataset.errorPhase;
    }
  }

  async function renderMarkdown(container, source, {typesetMath = true} = {}) {
    if (!container) return;
    return enqueue(async () => {
      const protectedResult = helpers.protectMathForMarkdown
        ? helpers.protectMathForMarkdown(String(source || ''))
        : {markdown: String(source || ''), formulas: []};
      container.innerHTML = sanitize(markedParse(protectedResult.markdown));
      restoreMath(container, protectedResult.formulas);
      if (typesetMath) {
        try { await typeset(container); }
        catch (error) { showRendererError(error); }
      }
    });
  }

  async function renderMathText(container, source) {
    if (!container) return;
    return enqueue(async () => {
      container.textContent = String(source || '');
      try { await typeset(container); }
      catch (error) { showRendererError(error); }
    });
  }

  async function renderMarkdownBatch(entries) {
    const active = entries.filter(entry => entry?.container?.isConnected);
    if (!active.length) return;
    return enqueue(async () => {
      for (const entry of active) {
        const protectedResult = helpers.protectMathForMarkdown
          ? helpers.protectMathForMarkdown(String(entry.source || ''))
          : {markdown: String(entry.source || ''), formulas: []};
        entry.container.innerHTML = sanitize(markedParse(protectedResult.markdown));
        restoreMath(entry.container, protectedResult.formulas);
      }
      try { await typesetMany(active.map(entry => entry.container)); }
      catch (error) { showRendererError(error); }
    });
  }

  async function renderMathTextBatch(entries) {
    const active = entries.filter(entry => entry?.container?.isConnected);
    if (!active.length) return;
    return enqueue(async () => {
      for (const entry of active) entry.container.textContent = String(entry.source || '');
      try { await typesetMany(active.map(entry => entry.container)); }
      catch (error) { showRendererError(error); }
    });
  }

  function markdownHtml(source) {
    const protectedResult = helpers.protectMathForMarkdown
      ? helpers.protectMathForMarkdown(String(source || ''))
      : {markdown: String(source || ''), formulas: []};
    const wrapper = document.createElement('div');
    wrapper.innerHTML = sanitize(markedParse(protectedResult.markdown));
    restoreMath(wrapper, protectedResult.formulas);
    return wrapper.innerHTML;
  }

  root.MathLabRender = {
    renderMarkdown,
    renderMarkdownBatch,
    renderMathText,
    renderMathTextBatch,
    markdownHtml,
    safeMarkdownPrefix: helpers.safeMarkdownPrefix || (value => String(value || '')),
    typeset,
  };
}(window));
