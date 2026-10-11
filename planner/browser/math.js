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

  function placeholder(formulas, display, source, environment = false) {
    const id = `math-${formulas.length}`;
    formulas.push({id, display, source, environment});
    return display
      ? `<div class="math-display" data-math-placeholder="${id}"></div>`
      : `<span class="math-inline" data-math-placeholder="${id}"></span>`;
  }

  function protectMathForMarkdown(value) {
    const source = String(value || '');
    const formulas = [];
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
        markdown += placeholder(formulas, true, source.slice(index, end), true);
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
        markdown += placeholder(formulas, true, source.slice(index + 2, closing).trim());
        index = closing + 2;
        continue;
      }

      if (source.startsWith('\\[', index) && !isEscaped(source, index)) {
        const closing = findUnescaped(source, '\\]', index + 2);
        if (closing < 0) {
          unclosedIndex = index;
          markdown += source.slice(index);
          break;
        }
        markdown += placeholder(formulas, true, source.slice(index + 2, closing).trim());
        index = closing + 2;
        continue;
      }

      if (source.startsWith('\\(', index) && !isEscaped(source, index)) {
        const closing = findUnescaped(source, '\\)', index + 2, true);
        if (closing < 0) {
          unclosedIndex = index;
          markdown += source.slice(index);
          break;
        }
        markdown += placeholder(formulas, false, source.slice(index + 2, closing));
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
        markdown += placeholder(formulas, false, source.slice(index + 1, closing));
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
  let startupFailure = null;

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
  }

  function markedParse(source) {
    if (!root.marked) return `<pre>${escapeHtml(source)}</pre>`;
    const options = {gfm: true, breaks: false};
    // MathLab accepts Markdown, not arbitrary user HTML.  Preserve only the
    // synthetic formula placeholders inserted by protectMathForMarkdown and
    // render every other raw HTML token as visible text.
    if (typeof root.marked.Renderer === 'function') {
      const renderer = new root.marked.Renderer();
      renderer.html = raw => {
        const value = String(raw?.raw ?? raw ?? '').trim();
        const placeholderOpen = /^<(span|div) class="math-(inline|display)" data-math-placeholder="math-\d+">$/;
        const placeholderClose = /^<\/(span|div)>$/;
        return placeholderOpen.test(value) || placeholderClose.test(value)
          ? value
          : escapeHtml(value);
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

  function restoreMath(container, formulas) {
    for (const formula of formulas || []) {
      const node = container.querySelector(`[data-math-placeholder="${formula.id}"]`);
      if (!node) continue;
      if (formula.environment) node.textContent = formula.source;
      else if (formula.display) node.textContent = `\\[${formula.source}\\]`;
      else node.textContent = `\\(${formula.source}\\)`;
    }
  }

  async function waitForMathJax() {
    if (startupFailure) throw startupFailure;
    if (!root.MathJax) throw new Error('MathJax 未加载');
    try {
      if (root.MathJax.startup?.promise) await root.MathJax.startup.promise;
    } catch (error) {
      startupFailure = error;
      throw error;
    }
  }

  async function typeset(container) {
    await waitForMathJax();
    if (typeof root.MathJax.typesetClear === 'function') root.MathJax.typesetClear([container]);
    if (typeof root.MathJax.typesetPromise === 'function') await root.MathJax.typesetPromise([container]);
  }

  function enqueue(task) {
    renderChain = renderChain.then(task, task);
    return renderChain;
  }

  function showRendererError(error) {
    const banner = document.getElementById('math-render-error');
    if (banner) {
      banner.textContent = `数学渲染引擎未能初始化：${error?.message || error}`;
      banner.classList.remove('hidden');
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
    renderMathText,
    markdownHtml,
    safeMarkdownPrefix: helpers.safeMarkdownPrefix || (value => String(value || '')),
    typeset,
  };
}(window));
