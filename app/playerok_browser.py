from __future__ import annotations

import json
import os
import re
import weakref
import shiboken6
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, quote

from PySide6.QtCore import QTimer, QUrl, Signal, Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QSpinBox, QPlainTextEdit, QSplitter, QMessageBox, QDialog,
)
from integrations.playerok.browser_sales import BrowserSalesReader, BrowserViewerChecker
from integrations.playerok.browser_chat_api import WebEngineGraphQLTransport
from integrations.playerok.graphql_http import PlayerokBrowserGraphQLSalesReader

try:
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
    WEBENGINE_AVAILABLE = True
except Exception:  # CI/headless installs may not ship QtWebEngine.
    QWebEngineView = QWebEngineProfile = QWebEnginePage = None
    WEBENGINE_AVAILABLE = False


PLAYEROK_ORIGIN = "https://playerok.com"
PLAYEROK_FIXED_ROUTES = {"Чаты": "/chats"}
PLAYEROK_PROFILE_ROUTE_LABELS = (
    "Мои объявления",
    "Продажи: Оплаченные (PAID)",
    "Продажи: Отправленные (SENT)",
    "Продажи: Завершённые (CONFIRMED)",
    "Продажи: Возврат средств",
)


_DOM_DIAGNOSTIC_SCRIPT = r"""(() => {
  const MAX_SCAN = 12000;
  const MAX_ATTR = 120;
  const MAX_CONTAINERS = 40;
  const MAX_MESSAGES = 100;
  const MAX_INTERACTIVE = 100;
  const MAX_DYNAMIC = 40;
  const MAX_ATTRIBUTES_PER_NODE = 64;
  const MAX_TEXTAREAS = 2;
  const MAX_DEAL_LINKS = 8;
  const MAX_CONTEXT_ANCESTORS = 4;
  const MAX_CONTEXT_SIBLINGS = 4;
  const MAX_COMPOSER_CONTROLS = 20;
  const MAX_REPEAT_GROUPS = 10;
  const MAX_REPEAT_EXAMPLES = 4;
  const MAX_STRUCTURAL_CHILDREN = 3;
  const MAX_STRUCTURAL_DEPTH = 2;
  const MAX_REPORT_BYTES = 192 * 1024;
  const MAX_INTERACTIVE_RECORDS = 1000;
  const safeAttr = value => {
    if (typeof value !== 'string' || !value) return null;
    if (/https?:\/\/|steamcommunity|(?:^|[?&])[^=\s]+=[^&\s]+/i.test(value)) return null;
    return value.length > MAX_ATTR ? value.slice(0, MAX_ATTR) : value;
  };
  const safeAria = (name, value) => {
    const clean = safeAttr(value);
    if (clean === null) return null;
    if (name === 'aria-label') {
      return /^(chat|message|messages|conversation|send|attach|чат|сообщения?|отправить|вложить)([ _-][a-zа-я0-9_-]+){0,2}$/i.test(clean)
        ? clean : '[redacted-label]';
    }
    const states = {
      'aria-live': ['off', 'polite', 'assertive'],
      'aria-expanded': ['true', 'false'], 'aria-hidden': ['true', 'false'],
      'aria-disabled': ['true', 'false'], 'aria-selected': ['true', 'false'],
      'aria-checked': ['true', 'false', 'mixed'], 'aria-busy': ['true', 'false'],
      'aria-current': ['page', 'step', 'location', 'date', 'time', 'true', 'false'],
    };
    if (states[name]) return states[name].includes(clean.toLowerCase()) ? clean.toLowerCase() : '[redacted]';
    if (['aria-controls', 'aria-owns', 'aria-activedescendant', 'aria-describedby', 'aria-labelledby'].includes(name)) {
      return /^[\w.:-]{1,120}(?:\s+[\w.:-]{1,120})*$/.test(clean) ? '[present]' : '[redacted]';
    }
    return '[redacted]';
  };
  const safeRole = value => {
    const allowed = new Set('alert alertdialog application article banner blockquote button caption cell checkbox code columnheader combobox complementary contentinfo definition dialog directory document feed figure form grid gridcell group heading img link list listbox listitem log main marquee math menu menubar menuitem menuitemcheckbox menuitemradio meter navigation none note option paragraph presentation progressbar radio radiogroup region row rowgroup rowheader scrollbar search searchbox separator slider spinbutton status switch tab table tablist tabpanel term textbox timer toolbar tooltip tree treegrid treeitem'.split(' '));
    const role = safeAttr(value || '');
    return role && allowed.has(role.toLowerCase()) ? role.toLowerCase() : null;
  };
  const describe = element => {
    const aria = {};
    const dataAttributes = [];
    let ariaCount = 0;
    const attributes = element.attributes || [];
    for (let attributeIndex = 0;
         attributeIndex < attributes.length && attributeIndex < MAX_ATTRIBUTES_PER_NODE;
         attributeIndex++) {
      const attribute = attributes[attributeIndex];
      const name = attribute.name.toLowerCase();
      if (name.startsWith('aria-') && ariaCount < 6) {
        aria[name] = safeAria(name, attribute.value);
        ariaCount++;
      } else if (name.startsWith('data-') && dataAttributes.length < 6) {
        dataAttributes.push(name);
      }
    }
    const classes = typeof element.className === 'string' ? element.className : '';
    return {
      tag: element.tagName.toLowerCase(),
      idPresent: element.hasAttribute('id'),
      class: safeAttr(classes),
      role: safeRole(element.getAttribute('role') || ''),
      aria,
      dataAttributes,
      childElementCount: Math.min(element.childElementCount || 0, MAX_SCAN),
    };
  };
  const describeStructural = element => {
    const summary = describe(element);
    const structuralAria = {};
    for (const [name, value] of Object.entries(summary.aria)) {
      structuralAria[name] = [
        'aria-controls', 'aria-owns', 'aria-activedescendant', 'aria-describedby', 'aria-labelledby',
      ].includes(name) && value !== null ? '[present]' : value;
    }
    const stableAttributePresence = {};
    const attributes = element.attributes || [];
    for (let i = 0; i < attributes.length && i < MAX_ATTRIBUTES_PER_NODE; i++) {
      const name = attributes[i].name.toLowerCase();
      if (name === 'id' || /^(data-(message|chat|deal|item|record|entity|test|key)[-_]?(id|key|testid)?|data-testid)$/.test(name)) {
        stableAttributePresence[name] = true;
      }
    }
    return {
      tag: summary.tag,
      class: summary.class,
      role: summary.role,
      aria: structuralAria,
      dataAttributes: summary.dataAttributes,
      childElementCount: summary.childElementCount,
      stableAttributePresence,
    };
  };
  const structuralTree = (element, depth, excluded, scanned) => {
    const children = [];
    if (depth > 0) {
      const elementChildren = element.children;
      for (let i = 0; i < elementChildren.length && children.length < MAX_STRUCTURAL_CHILDREN; i++) {
        if (scanned.has(elementChildren[i]) && !excluded(elementChildren[i])) {
          children.push(structuralTree(elementChildren[i], depth - 1, excluded, scanned));
        }
      }
    }
    return {
      node: describeStructural(element),
      children,
      childrenTruncated: Math.max(0, element.childElementCount - children.length) > 0,
    };
  };
  const body = document.body;
  const containers = [];
  const messages = [];
  const interactive = [];
  const dynamic = [];
  let elementCount = 0;
  let maxDepth = 0;
  let scanTruncated = false;
  let linkCount = 0;
  let linkCountExact = true;
  const textareas = [];
  const dealLinkElements = [];
  const interactiveRecords = [];
  const candidateCounts = {
    containers: 0, messages: 0, interactive: 0, textareas: 0,
    dealLinks: 0, repeatedSiblingGroups: 0, dynamicAreas: 0,
  };
  const allElements = [];
  const summaries = new Map();
  if (body) {
    const stack = [{element: body, depth: 1}];
    while (stack.length) {
      if (elementCount >= MAX_SCAN) { scanTruncated = true; break; }
      const current = stack.pop();
      const element = current.element;
      const depth = current.depth;
      elementCount++;
      allElements.push(element);
      if (depth > maxDepth) maxDepth = depth;
      const summary = describe(element);
      summaries.set(element, summary);
      const marker = `${element.id || ''} ${summary.class || ''} ${summary.role || ''}`.toLowerCase();
      if (/chat|message|conversation|thread|inbox/.test(marker) &&
          (element.childElementCount >= 2 || /^(main|section|ul|ol)$/.test(summary.tag))) {
        candidateCounts.containers++;
        if (containers.length < MAX_CONTAINERS) containers.push(summary);
      }
      if (/message|chat[-_ ]?item|bubble/.test(marker) ||
          summary.role === 'article' || summary.role === 'listitem') {
        candidateCounts.messages++;
        if (messages.length < MAX_MESSAGES) messages.push(summary);
      }
      if (element.matches('button,input,textarea,select,a[href],[role="button"],[role="textbox"]')) {
        candidateCounts.interactive++;
        const control = summary;
        if (/^(input|button)$/.test(summary.tag)) {
          control.type = safeAttr(element.getAttribute('type') || '');
        }
        if (interactive.length < MAX_INTERACTIVE) interactive.push(control);
        if (interactiveRecords.length < MAX_INTERACTIVE_RECORDS) {
          interactiveRecords.push({element, summary: control});
        }
      }
      if (summary.tag === 'textarea') {
        candidateCounts.textareas++;
        if (textareas.length < MAX_TEXTAREAS) textareas.push({element, summary});
      }
      if (element.matches('a[href]')) {
        linkCount++;
        try {
          const url = new URL(element.href, location.href);
          if (['playerok.com', 'www.playerok.com'].includes(url.hostname)) {
            if (dealLinkElements.length < MAX_DEAL_LINKS &&
                /^\/deal\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(url.pathname)) {
              dealLinkElements.push({element, summary, hostname: url.hostname, pathname: url.pathname});
            }
            if (/^\/deal\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(url.pathname)) {
              candidateCounts.dealLinks++;
            }
          }
        } catch (_) {}
      }
      const ariaLive = element.getAttribute('aria-live');
      if (element.id === 'root' || element.id === '__next' ||
          element.hasAttribute('data-reactroot') || ariaLive !== null ||
          summary.role === 'log') {
        candidateCounts.dynamicAreas++;
        if (dynamic.length < MAX_DYNAMIC) {
          dynamic.push({kind: ariaLive !== null || summary.role === 'log' ? 'dynamic' : 'spa-root', ...summary});
        }
      }
      const children = element.children;
      const remaining = Math.max(0, MAX_SCAN - elementCount - stack.length);
      if (children.length > remaining) scanTruncated = true;
      for (let index = Math.min(children.length, remaining) - 1; index >= 0; index--) {
        stack.push({element: children[index], depth: depth + 1});
      }
    }
    if (stack.length) scanTruncated = true;
  }
  linkCountExact = !scanTruncated;
  const scannedElements = new Set(allElements);
  const context = (element, ancestorLimit) => {
    const ancestors = [];
    let branch = element;
    let parent = element.parentElement;
    let depth = 1;
    while (parent && ancestors.length < ancestorLimit) {
      const children = parent.children;
      let childIndex = -1;
      for (let i = 0; i < children.length; i++) {
        if (children[i] === branch) { childIndex = i; break; }
      }
      const siblings = [];
      if (childIndex >= 0) {
        const start = Math.max(0, childIndex - Math.floor(MAX_CONTEXT_SIBLINGS / 2));
        const end = Math.min(children.length, start + MAX_CONTEXT_SIBLINGS);
        for (let i = start; i < end; i++) {
          if (i !== childIndex) siblings.push({index: i, node: describe(children[i])});
        }
      }
      ancestors.push({
        depth,
        node: describe(parent),
        childIndex: childIndex < 0 ? null : childIndex,
        childElementCount: Math.min(children.length, MAX_SCAN),
        siblingsTruncated: Math.max(0, children.length - 1) > siblings.length,
        siblings,
      });
      branch = parent;
      parent = parent.parentElement;
      depth++;
    }
    return ancestors;
  };
  const textareaContexts = textareas.map(({element, summary}) => ({
    textarea: summary,
    ancestors: context(element, MAX_CONTEXT_ANCESTORS),
  }));
  const composerScopes = [];
  const composerScopeElements = [];
  for (let textareaIndex = 0; textareaIndex < textareas.length; textareaIndex++) {
    const textarea = textareas[textareaIndex].element;
    let scope = textarea;
    let scopeDepth = 0;
    let cursor = textarea.parentElement;
    while (cursor && scopeDepth < MAX_CONTEXT_ANCESTORS) {
      const hasControl = interactiveRecords.some(record =>
        record.element !== textarea && cursor.contains(record.element) &&
        (record.element.matches('button,[role="button"]') ||
         (record.element.matches('input') && record.element.type === 'file')));
      if (hasControl) { scope = cursor; scopeDepth++; break; }
      scope = cursor;
      cursor = cursor.parentElement;
      scopeDepth++;
    }
    composerScopeElements.push(scope);
    const controls = [];
    let controlsFound = 0;
    for (const record of interactiveRecords) {
      if (!scope.contains(record.element) || record.element === textarea) continue;
      controlsFound++;
      if (controls.length >= MAX_COMPOSER_CONTROLS) continue;
      const type = (record.element.getAttribute('type') || '').toLowerCase();
      const ariaLabel = record.summary.aria['aria-label'];
      const signals = [];
      if (record.element.tagName === 'BUTTON' && type === 'submit') signals.push('explicit-submit-type');
      if (ariaLabel === 'send' || ariaLabel === 'отправить') signals.push('generic-send-label');
      if (record.element.tagName === 'INPUT' && type === 'file') signals.push('file-input');
      if (ariaLabel === 'attach' || ariaLabel === 'вложить') signals.push('generic-attach-label');
      controls.push({node: record.summary, signals});
    }
    composerScopes.push({textareaIndex, scopeDepth, scope: describe(scope), controls, controlsFound,
      controlsTruncated: controlsFound > controls.length});
  }
  const excludedRoots = new Set();
  const addExclusionRoot = element => {
    for (const root of excludedRoots) {
      if (root === element || root.contains(element)) return;
    }
    for (const root of [...excludedRoots]) {
      if (element.contains(root)) excludedRoots.delete(root);
    }
    excludedRoots.add(element);
  };
  for (const element of composerScopeElements) addExclusionRoot(element);
  for (const element of allElements) {
    const summary = summaries.get(element) || describe(element);
    const role = (summary.role || '').toLowerCase();
    const marker = `${element.id || ''} ${summary.class || ''}`.toLowerCase();
    if (/^(nav|header|footer|aside)$/.test(summary.tag) ||
        ['navigation', 'banner', 'complementary', 'contentinfo'].includes(role) ||
        /(?:^|[-_\s])(site-nav|navbar|sidebar|site-menu)(?:$|[-_\s])/.test(marker)) {
      addExclusionRoot(element);
    }
  }
  for (const deal of dealLinkElements) {
    addExclusionRoot(deal.element);
    let ancestor = deal.element.parentElement;
    for (let depth = 0; ancestor && depth < 2; depth++, ancestor = ancestor.parentElement) {
      if (!textareas.some(item => ancestor.contains(item.element)) && ancestor.childElementCount <= 12) {
        addExclusionRoot(ancestor);
      }
    }
  }
  const isExcluded = (() => {
    const excludedElements = new Set();
    for (const root of excludedRoots) {
      if (!scannedElements.has(root)) continue;
      const stack = [root];
      while (stack.length && excludedElements.size < MAX_SCAN) {
        const current = stack.pop();
        if (excludedElements.has(current)) continue;
        excludedElements.add(current);
        const children = current.children;
        for (let i = 0; i < children.length && i < MAX_SCAN; i++) {
          if (scannedElements.has(children[i])) stack.push(children[i]);
        }
      }
    }
    return element => excludedElements.has(element);
  })();
  const structuralContext = (element, ancestorLimit) => {
    const ancestors = [];
    let branch = element;
    let parent = element.parentElement;
    let depth = 1;
    while (parent && ancestors.length < ancestorLimit) {
      const children = parent.children;
      let childIndex = -1;
      for (let i = 0; i < children.length; i++) {
        if (children[i] === branch) { childIndex = i; break; }
      }
      const siblings = [];
      if (childIndex >= 0) {
        const start = Math.max(0, childIndex - 2);
        const end = Math.min(children.length, start + 5);
        for (let i = start; i < end; i++) {
          if (i !== childIndex && scannedElements.has(children[i]) && !isExcluded(children[i])) {
            siblings.push({index: i, node: describeStructural(children[i])});
          }
        }
      }
      ancestors.push({
        depth,
        node: describeStructural(parent),
        childIndex: childIndex < 0 ? null : childIndex,
        childElementCount: Math.min(children.length, MAX_SCAN),
        siblingsTruncated: Math.max(0, children.length - 1) > siblings.length,
        siblings,
      });
      branch = parent;
      parent = parent.parentElement;
      depth++;
    }
    return ancestors;
  };
  const structuralSignature = element => {
    const summary = summaries.get(element) || describe(element);
    const classTokens = (summary.class || '').split(/\s+/).filter(Boolean).sort();
    const directChildren = [];
    const children = element.children;
    for (let i = 0; i < children.length && directChildren.length < MAX_STRUCTURAL_CHILDREN; i++) {
      if (!scannedElements.has(children[i]) || isExcluded(children[i])) continue;
      const child = summaries.get(children[i]) || describe(children[i]);
      directChildren.push([child.tag, (child.class || '').split(/\s+/).filter(Boolean).sort(), child.role,
        child.dataAttributes]);
    }
    return JSON.stringify([summary.tag, classTokens, summary.role, summary.dataAttributes, directChildren]);
  };
  const repeatedSiblingGroups = [];
  for (const parent of allElements) {
    if (isExcluded(parent)) continue;
    const grouped = new Map();
    const children = parent.children;
    for (let i = 0; i < children.length && i < MAX_SCAN; i++) {
      const child = children[i];
      if (!scannedElements.has(child) || isExcluded(child)) continue;
      const signature = structuralSignature(child);
      if (!grouped.has(signature)) grouped.set(signature, []);
      grouped.get(signature).push(child);
    }
    for (const [signature, matching] of grouped) {
      if (matching.length < 2) continue;
      candidateCounts.repeatedSiblingGroups++;
      if (repeatedSiblingGroups.length < MAX_REPEAT_GROUPS) {
        repeatedSiblingGroups.push({
          count: matching.length,
          parent: describeStructural(parent),
          ancestors: structuralContext(parent, 3),
          examples: matching.slice(0, MAX_REPEAT_EXAMPLES).map(element =>
            structuralTree(element, MAX_STRUCTURAL_DEPTH, isExcluded, scannedElements)),
          examplesTruncated: matching.length > MAX_REPEAT_EXAMPLES,
        });
      }
    }
  }
  const sectionTruncated = {
    body: scanTruncated,
    candidateContainers: containers.length < candidateCounts.containers,
    candidateMessages: messages.length < candidateCounts.messages,
    interactiveElements: interactive.length < candidateCounts.interactive,
    textareaContexts: textareas.length < candidateCounts.textareas ||
      textareaContexts.some(item => item.ancestors.some(level => level.siblingsTruncated)),
    dealLinks: dealLinkElements.length < candidateCounts.dealLinks,
    repeatedSiblingGroups: repeatedSiblingGroups.length < candidateCounts.repeatedSiblingGroups ||
      repeatedSiblingGroups.some(group => group.examplesTruncated ||
        group.ancestors.some(level => level.siblingsTruncated) ||
        group.examples.some(tree => tree.childrenTruncated)),
    composerControls: composerScopes.some(scope => scope.controlsTruncated),
    dynamicAreas: dynamic.length < candidateCounts.dynamicAreas,
  };
  const truncationReasons = [];
  const addReason = (reason, section) => {
    if (!truncationReasons.includes(reason)) truncationReasons.push(reason);
    if (section) sectionTruncated[section] = true;
  };
  if (scanTruncated) addReason('dom-scan-limit', 'body');
  if (sectionTruncated.candidateContainers) addReason('container-sample-limit', 'candidateContainers');
  if (sectionTruncated.candidateMessages) addReason('message-sample-limit', 'candidateMessages');
  if (sectionTruncated.interactiveElements) addReason('interactive-sample-limit', 'interactiveElements');
  if (sectionTruncated.textareaContexts) addReason('textarea-sample-limit', 'textareaContexts');
  if (sectionTruncated.dealLinks) addReason('deal-link-sample-limit', 'dealLinks');
  if (sectionTruncated.repeatedSiblingGroups) addReason('repeat-group-limit', 'repeatedSiblingGroups');
  if (sectionTruncated.dynamicAreas) addReason('dynamic-area-sample-limit', 'dynamicAreas');
  if (sectionTruncated.composerControls) addReason('composer-control-sample-limit', 'composerControls');
  const report = {
    schema: 'playerok-dom-structure-v1',
    status: 'ok',
    page: {site: 'playerok'},
    body: {elementCount, maxDepth, scanTruncated},
    counts: {...candidateCounts, links: linkCount, scannedElements: elementCount},
    candidateContainers: containers,
    candidateMessages: messages,
    textareaContexts,
    composerScopes,
    repeatedSiblingGroups,
    repeatedSiblingGroupsTruncated: sectionTruncated.repeatedSiblingGroups,
    interactiveElements: interactive,
    linkCount,
    linkCountExact,
    dynamicAreas: dynamic,
    candidateContainersTruncated: sectionTruncated.candidateContainers,
    candidateMessagesTruncated: sectionTruncated.candidateMessages,
    interactiveElementsTruncated: sectionTruncated.interactiveElements,
    dynamicAreasTruncated: sectionTruncated.dynamicAreas,
    truncation: {truncated: truncationReasons.length > 0, reasons: truncationReasons, sections: sectionTruncated},
  };
  const encodedSize = text => {
    try { return new TextEncoder().encode(text).length; }
    catch (_) { return text.length * 2; }
  };
  const markJsonTruncation = section => {
    addReason('total-json-byte-limit', section);
    report.truncation.truncated = true;
    report.truncation.reasons = truncationReasons;
    report.truncation.sections = sectionTruncated;
  };
  const shrinkArray = (array, max, section) => {
    if (!Array.isArray(array) || array.length <= max) return;
    array.length = max;
    markJsonTruncation(section);
  };
  const shrinkTree = (tree, depth, childLimit) => {
    if (!tree || !Array.isArray(tree.children)) return;
    if (depth <= 0) {
      if (tree.children.length) tree.childrenTruncated = true;
      tree.children = [];
      return;
    }
    if (tree.children.length > childLimit) {
      tree.children.length = childLimit;
      tree.childrenTruncated = true;
    }
    for (const child of tree.children) shrinkTree(child, depth - 1, childLimit);
  };
  const stages = [
    () => shrinkArray(report.dynamicAreas, 12, 'dynamicAreas'),
    () => shrinkArray(report.candidateContainers, 12, 'candidateContainers'),
    () => shrinkArray(report.candidateMessages, 24, 'candidateMessages'),
    () => shrinkArray(report.interactiveElements, 24, 'interactiveElements'),
    () => {
      for (const scope of report.composerScopes) {
        const countBefore = scope.controls.length;
        shrinkArray(scope.controls, 8, 'composerControls');
        if (scope.controls.length < countBefore) scope.controlsTruncated = true;
      }
    },
    () => {
      for (const item of report.textareaContexts) {
        shrinkArray(item.ancestors, 2, 'textareaContexts');
        for (const ancestor of item.ancestors) shrinkArray(ancestor.siblings, 2, 'textareaContexts');
      }
    },
    () => {
      shrinkArray(report.repeatedSiblingGroups, 6, 'repeatedSiblingGroups');
      for (const group of report.repeatedSiblingGroups) {
        const examplesBefore = group.examples.length;
        shrinkArray(group.examples, 2, 'repeatedSiblingGroups');
        if (group.examples.length < examplesBefore) group.examplesTruncated = true;
        shrinkArray(group.ancestors, 2, 'repeatedSiblingGroups');
        for (const tree of group.examples) shrinkTree(tree, 1, 2);
      }
    },
    () => shrinkArray(report.candidateMessages, 8, 'candidateMessages'),
    () => shrinkArray(report.interactiveElements, 8, 'interactiveElements'),
    () => shrinkArray(report.dynamicAreas, 0, 'dynamicAreas'),
    () => shrinkArray(report.repeatedSiblingGroups, 0, 'repeatedSiblingGroups'),
    () => shrinkArray(report.candidateContainers, 0, 'candidateContainers'),
  ];
  try {
    let serialized = JSON.stringify(report);
    for (const stage of stages) {
      if (encodedSize(serialized) <= MAX_REPORT_BYTES) break;
      stage();
      report.truncation.truncated = true;
      report.truncation.reasons = truncationReasons;
      report.truncation.sections = sectionTruncated;
      report.repeatedSiblingGroupsTruncated = sectionTruncated.repeatedSiblingGroups;
      report.candidateContainersTruncated = sectionTruncated.candidateContainers;
      report.candidateMessagesTruncated = sectionTruncated.candidateMessages;
      report.interactiveElementsTruncated = sectionTruncated.interactiveElements;
      report.dynamicAreasTruncated = sectionTruncated.dynamicAreas;
      serialized = JSON.stringify(report);
    }
    if (encodedSize(serialized) <= MAX_REPORT_BYTES) return serialized;
    return JSON.stringify({
      schema: 'playerok-dom-structure-v1', status: 'error',
      error: {code: 'json-byte-budget-unmet'},
      counts: {...candidateCounts, links: linkCount, scannedElements: elementCount},
      truncation: {truncated: true, reasons: ['total-json-byte-limit', 'json-byte-budget-unmet'], sections: sectionTruncated},
    });
  } catch (_) {
    return JSON.stringify({
      schema: 'playerok-dom-structure-v1', status: 'error',
      error: {code: 'serialization-error'},
      truncation: {truncated: true, reasons: ['serialization-error'], sections: {}},
    });
  }
})()"""

_DOM_DIAGNOSTIC_MAX_BYTES = 192 * 1024
_DOM_DIAGNOSTIC_CALLBACK_MAX_BYTES = 256 * 1024
_DOM_DIAGNOSTIC_ERROR_CODES = {
    "callback-result-not-string", "callback-byte-limit", "invalid-json", "invalid-root",
    "invalid-page-metadata", "invalid-body-metadata", "json-byte-budget-unmet",
    "serialization-error", "javascript-evaluation-error", "sanitized-json-byte-limit",
    "invalid-diagnostic-status",
}


def _dom_safe_attribute(value: object, *, limit: int = 120) -> str | None:
    if type(value) is not str or not value:
        return None
    if "https://" in value.lower() or "http://" in value.lower() or "steamcommunity" in value.lower():
        return None
    if any(marker in value.lower() for marker in ("token=", "partner=", "cookie", "authorization")):
        return None
    return value[:limit]


def _sanitize_dom_node(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    tag = value.get("tag")
    if type(tag) is not str or not tag.isascii() or not tag.replace("-", "").isalnum():
        return None
    aria = {}
    raw_aria = value.get("aria")
    if isinstance(raw_aria, dict):
        for name, attribute_value in list(raw_aria.items())[:8]:
            if type(name) is not str or not name.startswith("aria-") or len(name) > 64:
                continue
            clean = _dom_safe_attribute(attribute_value)
            if name == "aria-label" and clean is not None and not re.fullmatch(
                r"(?i)(chat|message|messages|conversation|send|attach|чат|сообщения?|отправить|вложить)([ _-][a-zа-я0-9_-]+){0,2}", clean
            ):
                clean = "[redacted-label]"
            elif name in {"aria-live", "aria-expanded", "aria-hidden", "aria-disabled", "aria-selected", "aria-checked", "aria-busy", "aria-current"}:
                allowed = {
                    "aria-live": {"off", "polite", "assertive"},
                    "aria-expanded": {"true", "false"},
                    "aria-hidden": {"true", "false"},
                    "aria-disabled": {"true", "false"},
                    "aria-selected": {"true", "false"},
                    "aria-checked": {"true", "false", "mixed"},
                    "aria-busy": {"true", "false"},
                    "aria-current": {"page", "step", "location", "date", "time", "true", "false"},
                }
                clean = clean.lower() if clean and clean.lower() in allowed[name] else "[redacted]"
            elif name in {"aria-controls", "aria-owns", "aria-activedescendant", "aria-describedby", "aria-labelledby"}:
                clean = "[present]" if clean is not None else None
            else:
                clean = "[redacted]" if clean is not None else None
            aria[name] = clean
    data_names = []
    raw_data = value.get("dataAttributes")
    if isinstance(raw_data, list):
        data_names = [
            name[:64] for name in raw_data[:8]
            if type(name) is str and re.fullmatch(r"data-[a-z0-9_-]{1,60}", name)
        ]
    role = _dom_safe_attribute(value.get("role"), limit=64)
    allowed_roles = {
        "alert", "alertdialog", "application", "article", "banner", "blockquote", "button",
        "caption", "cell", "checkbox", "code", "columnheader", "combobox", "complementary",
        "contentinfo", "definition", "dialog", "directory", "document", "feed", "figure", "form",
        "grid", "gridcell", "group", "heading", "img", "link", "list", "listbox", "listitem",
        "log", "main", "marquee", "math", "menu", "menubar", "menuitem", "menuitemcheckbox",
        "menuitemradio", "meter", "navigation", "none", "note", "option", "paragraph",
        "presentation", "progressbar", "radio", "radiogroup", "region", "row", "rowgroup",
        "rowheader", "scrollbar", "search", "searchbox", "separator", "slider", "spinbutton",
        "status", "switch", "tab", "table", "tablist", "tabpanel", "term", "textbox", "timer",
        "toolbar", "tooltip", "tree", "treegrid", "treeitem",
    }
    role = role.lower() if role and role.lower() in allowed_roles else None
    result = {
        "tag": tag[:32].lower(),
        "id_attribute_present": value.get("idPresent") is True or (
            type(value.get("id")) is str and bool(value.get("id"))
        ),
        "class": _dom_safe_attribute(value.get("class")),
        "role": role,
        "aria": aria,
        "data_attribute_names": data_names,
        "child_element_count": _dom_bounded_int(value.get("childElementCount"), 0, 12000),
    }
    if "type" in value:
        result["type"] = _dom_safe_attribute(value.get("type"), limit=32)
    if "kind" in value and value.get("kind") in {"dynamic", "spa-root"}:
        result["kind"] = value["kind"]
    return result


def _dom_bounded_int(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        return minimum
    return max(minimum, min(value, maximum))


def _sanitize_dom_context_chain(value: object, *, max_depth: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for level in value[:max_depth]:
        if not isinstance(level, dict):
            continue
        node = _sanitize_dom_node(level.get("node"))
        if node is None:
            continue
        siblings = []
        raw_siblings = level.get("siblings")
        if isinstance(raw_siblings, list):
            for sibling in raw_siblings[:4]:
                if not isinstance(sibling, dict):
                    continue
                sibling_node = _sanitize_dom_node(sibling.get("node"))
                if sibling_node is not None:
                    siblings.append({
                        "index": _dom_bounded_int(sibling.get("index"), 0, 12000),
                        "node": sibling_node,
                    })
        child_index = level.get("childIndex")
        result.append({
            "depth": _dom_bounded_int(level.get("depth"), 1, max_depth),
            "node": node,
            "child_index": _dom_bounded_int(child_index, 0, 12000) if type(child_index) is int else None,
            "child_element_count": _dom_bounded_int(level.get("childElementCount"), 0, 12000),
            "siblings_truncated": level.get("siblingsTruncated") is True,
            "siblings": siblings,
        })
    return result


def _sanitize_dom_structure_node(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    normalized = _sanitize_dom_node({
        "tag": value.get("tag"),
        "class": value.get("class"),
        "role": value.get("role"),
        "aria": value.get("aria"),
        "dataAttributes": value.get("dataAttributes"),
        "childElementCount": value.get("childElementCount"),
    })
    if normalized is None:
        return None
    normalized.pop("id", None)
    for name in ("aria-controls", "aria-owns", "aria-activedescendant", "aria-describedby", "aria-labelledby"):
        if normalized.get("aria", {}).get(name) is not None:
            normalized["aria"][name] = "[present]"
    stable_presence = {}
    raw_stable = value.get("stableAttributePresence")
    if isinstance(raw_stable, dict):
        for name, present in list(raw_stable.items())[:12]:
            if type(name) is str and present is True and (
                name == "id" or re.fullmatch(r"data-[a-z0-9_-]{1,60}", name)
            ):
                stable_presence[name] = True
    normalized["stable_attribute_presence"] = stable_presence
    normalized["id_attribute_present"] = stable_presence.get("id", False)
    return normalized


def _sanitize_dom_structure_tree(value: object, *, depth: int) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    node = _sanitize_dom_structure_node(value.get("node"))
    if node is None:
        return None
    children = []
    raw_children = value.get("children")
    if depth > 0 and isinstance(raw_children, list):
        for child in raw_children[:3]:
            sanitized = _sanitize_dom_structure_tree(child, depth=depth - 1)
            if sanitized is not None:
                children.append(sanitized)
    return {
        "node": node,
        "children": children,
        "children_truncated": value.get("childrenTruncated") is True,
    }


def _dom_structure_signature(tree: dict[str, Any]) -> tuple[Any, ...]:
    node = tree["node"]
    class_tokens = tuple(sorted((node.get("class") or "").split()))
    child_shapes = tuple(
        (
            child["node"].get("tag"),
            tuple(sorted((child["node"].get("class") or "").split())),
            child["node"].get("role"),
            tuple(child["node"].get("data_attribute_names", [])),
        )
        for child in tree["children"]
    )
    return (
        node.get("tag"), class_tokens, node.get("role"),
        tuple(node.get("data_attribute_names", [])), child_shapes,
    )


def _sanitize_repeated_sibling_groups(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for group in value[:10]:
        if not isinstance(group, dict) or group.get("excluded") is True:
            continue
        parent = _sanitize_dom_structure_node(group.get("parent"))
        if parent is None:
            continue
        examples = []
        raw_examples = group.get("examples")
        if isinstance(raw_examples, list):
            for example in raw_examples[:4]:
                tree = _sanitize_dom_structure_tree(example, depth=2)
                if tree is not None:
                    examples.append(tree)
        signatures = {_dom_structure_signature(example) for example in examples}
        count = _dom_bounded_int(group.get("count"), 0, 12000)
        if count < 2 or len(examples) < 2 or len(signatures) != 1:
            continue
        raw_ancestors = group.get("ancestors")
        ancestors_truncated = isinstance(raw_ancestors, list) and any(
            level.get("siblingsTruncated") is True for level in raw_ancestors if isinstance(level, dict)
        )
        result.append({
            "classification": "structural-candidate",
            "count": count,
            "descendant_depth": 2,
            "examples_truncated": group.get("examplesTruncated") is True,
            "siblings_truncated": ancestors_truncated,
            "parent": parent,
            "ancestors": _sanitize_dom_structure_context_chain(group.get("ancestors"), max_depth=3),
            "examples": examples,
        })
    return result


def _sanitize_dom_structure_context_chain(value: object, *, max_depth: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for level in value[:max_depth]:
        if not isinstance(level, dict):
            continue
        node = _sanitize_dom_structure_node(level.get("node"))
        if node is None:
            continue
        siblings = []
        raw_siblings = level.get("siblings")
        if isinstance(raw_siblings, list):
            for sibling in raw_siblings[:4]:
                if not isinstance(sibling, dict):
                    continue
                sibling_node = _sanitize_dom_structure_node(sibling.get("node"))
                if sibling_node is not None:
                    siblings.append({
                        "index": _dom_bounded_int(sibling.get("index"), 0, 12000),
                        "node": sibling_node,
                    })
        result.append({
            "depth": _dom_bounded_int(level.get("depth"), 1, max_depth),
            "node": node,
            "child_index": _dom_bounded_int(level.get("childIndex"), 0, 12000)
                if type(level.get("childIndex")) is int else None,
            "child_element_count": _dom_bounded_int(level.get("childElementCount"), 0, 12000),
            "siblings_truncated": level.get("siblingsTruncated") is True,
            "siblings": siblings,
        })
    return result


def _dom_error_summary(reason: str, counts: object = None) -> dict[str, Any]:
    if reason not in _DOM_DIAGNOSTIC_ERROR_CODES:
        reason = "serialization-error"
    raw_counts = counts if isinstance(counts, dict) else {}
    safe_counts = {
        key: _dom_bounded_int(raw_counts.get(key), 0, 12000)
        for key in (
            "containers", "messages", "interactive", "textareas", "dealLinks",
            "repeatedSiblingGroups", "dynamicAreas", "links", "scannedElements",
        )
    }
    return {
        "schema": "playerok-dom-structure-v1",
        "status": "error",
        "page": {"site": "playerok"},
        "body": {"element_count_scanned": safe_counts["scannedElements"], "scan_truncated": True},
        "counts": safe_counts,
        "candidate_containers": [],
        "candidate_messages": [],
        "interactive_elements": [],
        "textarea_contexts": [],
        "composer_scopes": [],
        "repeated_sibling_candidates": [],
        "dynamic_areas": [],
        "error": {"code": reason},
        "truncated": {"occurred": True, "reasons": [reason], "sections": {}},
    }


def _dom_summary_size(value: dict[str, Any]) -> int:
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    return len(rendered.encode("utf-8"))


def _fit_dom_summary(value: dict[str, Any], *, max_bytes: int = _DOM_DIAGNOSTIC_MAX_BYTES) -> dict[str, Any]:
    """Trim secondary details until the exact displayed JSON fits its byte budget."""
    summary = value
    truncation = summary.setdefault("truncated", {"occurred": False, "reasons": [], "sections": {}})
    reasons = truncation.setdefault("reasons", [])
    sections = truncation.setdefault("sections", {})

    def mark(section: str) -> None:
        truncation["occurred"] = True
        if "total-json-byte-limit" not in reasons:
            reasons.append("total-json-byte-limit")
        sections[section] = True

    def trim_list(items: object, limit: int, section: str) -> None:
        if isinstance(items, list) and len(items) > limit:
            del items[limit:]
            mark(section)

    def trim_tree(tree: object, depth: int, child_limit: int) -> None:
        if not isinstance(tree, dict):
            return
        children = tree.get("children")
        if not isinstance(children, list):
            return
        if depth <= 0:
            if children:
                tree["children_truncated"] = True
                children.clear()
                mark("repeated_sibling_candidates")
            return
        trim_list(children, child_limit, "repeated_sibling_candidates")
        for child in children:
            trim_tree(child, depth - 1, child_limit)

    def stages() -> list[tuple[str, Any]]:
        return [
            ("dynamic_areas", lambda: trim_list(summary.get("dynamic_areas"), 12, "dynamic_areas")),
            ("candidate_containers", lambda: trim_list(summary.get("candidate_containers"), 12, "candidate_containers")),
            ("candidate_messages", lambda: trim_list(summary.get("candidate_messages"), 24, "candidate_messages")),
            ("interactive_elements", lambda: trim_list(summary.get("interactive_elements"), 24, "interactive_elements")),
            ("composer_scopes", lambda: [trim_list(scope.get("controls"), 8, "composer_scopes")
                for scope in summary.get("composer_scopes", []) if isinstance(scope, dict)]),
            ("textarea_contexts", lambda: [
                (trim_list(item.get("ancestors"), 2, "textarea_contexts"),
                 [trim_list(level.get("siblings"), 2, "textarea_contexts")
                  for level in item.get("ancestors", []) if isinstance(level, dict)])
                for item in summary.get("textarea_contexts", []) if isinstance(item, dict)]),
            ("repeated_sibling_candidates", lambda: [
                (trim_list(summary.get("repeated_sibling_candidates"), 6, "repeated_sibling_candidates"),
                 trim_list(group.get("examples"), 2, "repeated_sibling_candidates"),
                 trim_list(group.get("ancestors"), 2, "repeated_sibling_candidates"),
                 [trim_tree(tree, 1, 2) for tree in group.get("examples", [])])
                for group in summary.get("repeated_sibling_candidates", []) if isinstance(group, dict)]),
            ("candidate_messages", lambda: trim_list(summary.get("candidate_messages"), 8, "candidate_messages")),
            ("interactive_elements", lambda: trim_list(summary.get("interactive_elements"), 8, "interactive_elements")),
            ("dynamic_areas", lambda: trim_list(summary.get("dynamic_areas"), 0, "dynamic_areas")),
            ("repeated_sibling_candidates", lambda: trim_list(summary.get("repeated_sibling_candidates"), 0, "repeated_sibling_candidates")),
            ("candidate_containers", lambda: trim_list(summary.get("candidate_containers"), 0, "candidate_containers")),
        ]

    try:
        for _section, stage in stages():
            if _dom_summary_size(summary) <= max_bytes:
                break
            stage()
        if _dom_summary_size(summary) <= max_bytes:
            return summary
    except (TypeError, ValueError, RecursionError, UnicodeError):
        return _dom_error_summary("serialization-error", summary.get("counts"))
    return _dom_error_summary("sanitized-json-byte-limit", summary.get("counts"))


def _sanitize_dom_summary(value: object) -> dict[str, Any]:
    """Return a bounded safe report or a compact structured diagnostic error."""
    if type(value) is not str:
        return _dom_error_summary("callback-result-not-string")
    if len(value) > _DOM_DIAGNOSTIC_CALLBACK_MAX_BYTES:
        return _dom_error_summary("callback-byte-limit")
    try:
        if len(value.encode("utf-8")) > _DOM_DIAGNOSTIC_CALLBACK_MAX_BYTES:
            return _dom_error_summary("callback-byte-limit")
        payload = json.loads(value)
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return _dom_error_summary("invalid-json")
    if not isinstance(payload, dict):
        return _dom_error_summary("invalid-root")
    if payload.get("status") == "error":
        raw_error = payload.get("error")
        code = raw_error.get("code") if isinstance(raw_error, dict) else None
        return _dom_error_summary(code if type(code) is str else "serialization-error", payload.get("counts"))

    raw_page = payload.get("page")
    if not isinstance(raw_page, dict):
        return _dom_error_summary("invalid-page-metadata")
    if raw_page.get("site") != "playerok":
        origin = raw_page.get("origin")
        path = raw_page.get("pathname")
        try:
            if type(origin) is not str or type(path) is not str:
                return _dom_error_summary("invalid-page-metadata")
            parsed_origin = urlsplit(origin)
            if (
                parsed_origin.scheme != "https"
                or parsed_origin.hostname not in {"playerok.com", "www.playerok.com"}
                or parsed_origin.username is not None
                or parsed_origin.password is not None
                or parsed_origin.port is not None
                or not path.startswith("/")
            ):
                return _dom_error_summary("invalid-page-metadata")
        except (TypeError, ValueError):
            return _dom_error_summary("invalid-page-metadata")

    raw_body = payload.get("body")
    if not isinstance(raw_body, dict):
        return _dom_error_summary("invalid-body-metadata", payload.get("counts"))

    raw_counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    def found_count(key: str, fallback: int) -> int:
        return _dom_bounded_int(raw_counts.get(key), 0, 12000) if type(raw_counts.get(key)) is int else fallback

    truncation_reasons = []
    known_reasons = {
        "dom-scan-limit", "container-sample-limit", "message-sample-limit",
        "interactive-sample-limit", "textarea-sample-limit", "deal-link-sample-limit",
        "repeat-group-limit", "dynamic-area-sample-limit", "composer-control-sample-limit",
        "total-json-byte-limit", "callback-byte-limit", "link-count-limit",
    }
    raw_truncation = payload.get("truncation")
    if isinstance(raw_truncation, dict) and isinstance(raw_truncation.get("reasons"), list):
        truncation_reasons.extend(
            reason for reason in raw_truncation["reasons"][:20]
            if type(reason) is str and reason in known_reasons
        )
    sections = {}
    raw_sections = raw_truncation.get("sections") if isinstance(raw_truncation, dict) else None
    if isinstance(raw_sections, dict):
        section_map = {
            "body": "body", "candidateContainers": "candidate_containers",
            "candidateMessages": "candidate_messages", "interactiveElements": "interactive_elements",
            "textareaContexts": "textarea_contexts", "dealLinks": "deal_links",
            "repeatedSiblingGroups": "repeated_sibling_candidates",
            "composerControls": "composer_scopes", "dynamicAreas": "dynamic_areas",
        }
        sections = {target: raw_sections.get(source) is True for source, target in section_map.items()}
    else:
        sections = {
            "body": raw_body.get("scanTruncated") is True,
            "candidate_containers": payload.get("candidateContainersTruncated") is True,
            "candidate_messages": payload.get("candidateMessagesTruncated") is True,
            "interactive_elements": payload.get("interactiveElementsTruncated") is True,
            "repeated_sibling_candidates": payload.get("repeatedSiblingGroupsTruncated") is True,
            "dynamic_areas": payload.get("dynamicAreasTruncated") is True,
        }

    def nodes(name: str, limit: int, section: str) -> list[dict[str, Any]]:
        raw_nodes = payload.get(name)
        if not isinstance(raw_nodes, list):
            return []
        if len(raw_nodes) > limit:
            sections[section] = True
            reason = f"{section.replace('_', '-')}-python-section-limit"
            if reason not in truncation_reasons:
                truncation_reasons.append(reason)
        result = []
        for item in raw_nodes[:limit]:
            node = _sanitize_dom_node(item)
            if node is not None:
                result.append(node)
        return result

    textarea_contexts = []
    raw_textareas = payload.get("textareaContexts")
    if isinstance(raw_textareas, list):
        if len(raw_textareas) > 2:
            sections["textarea_contexts"] = True
        for item in raw_textareas[:2]:
            if not isinstance(item, dict):
                continue
            textarea = _sanitize_dom_node(item.get("textarea"))
            if textarea is not None and textarea.get("tag") == "textarea":
                ancestors = _sanitize_dom_context_chain(item.get("ancestors"), max_depth=4)
                textarea_contexts.append({"textarea": textarea, "ancestors": ancestors})

    composer_scopes = []
    raw_scopes = payload.get("composerScopes")
    if isinstance(raw_scopes, list):
        if len(raw_scopes) > 2:
            sections["composer_scopes"] = True
        allowed_signals = {
            "explicit-submit-type", "generic-send-label", "file-input", "generic-attach-label",
        }
        for item in raw_scopes[:2]:
            if not isinstance(item, dict):
                continue
            scope = _sanitize_dom_node(item.get("scope"))
            if scope is None:
                continue
            controls = []
            raw_controls = item.get("controls")
            if isinstance(raw_controls, list):
                if len(raw_controls) > 20 or item.get("controlsTruncated") is True:
                    sections["composer_scopes"] = True
                for control in raw_controls[:20]:
                    if not isinstance(control, dict):
                        continue
                    node = _sanitize_dom_node(control.get("node"))
                    if node is None:
                        continue
                    raw_signals = control.get("signals")
                    signals = [
                        signal for signal in raw_signals[:4]
                        if type(signal) is str and signal in allowed_signals
                    ] if isinstance(raw_signals, list) else []
                    controls.append({"node": node, "signals": signals})
            composer_scopes.append({
                "textarea_index": _dom_bounded_int(item.get("textareaIndex"), 0, 1),
                "scope_depth": _dom_bounded_int(item.get("scopeDepth"), 0, 4),
                "controls_found": _dom_bounded_int(item.get("controlsFound"), 0, 1_000_000),
                "controls": controls,
            })

    counts = {
        "containers": found_count("containers", len(payload.get("candidateContainers", []))
            if isinstance(payload.get("candidateContainers"), list) else 0),
        "messages": found_count("messages", len(payload.get("candidateMessages", []))
            if isinstance(payload.get("candidateMessages"), list) else 0),
        "interactive": found_count("interactive", len(payload.get("interactiveElements", []))
            if isinstance(payload.get("interactiveElements"), list) else 0),
        "textareas": found_count("textareas", len(raw_textareas) if isinstance(raw_textareas, list) else 0),
        "deal_links": found_count("dealLinks", 0),
        "repeated_sibling_groups": found_count("repeatedSiblingGroups",
            len(payload.get("repeatedSiblingGroups", []))
            if isinstance(payload.get("repeatedSiblingGroups"), list) else 0),
        "dynamic_areas": found_count("dynamicAreas", len(payload.get("dynamicAreas", []))
            if isinstance(payload.get("dynamicAreas"), list) else 0),
        "links": found_count("links", _dom_bounded_int(payload.get("linkCount"), 0, 12000)),
        "scanned_elements": _dom_bounded_int(raw_body.get("elementCount"), 0, 12000),
    }
    raw_link_count = payload.get("linkCount")
    if type(raw_link_count) is int and raw_link_count > 12000:
        sections["links"] = True
        truncation_reasons.append("link-count-limit")
    raw_groups = payload.get("repeatedSiblingGroups")
    groups = _sanitize_repeated_sibling_groups(raw_groups)
    if isinstance(raw_groups, list) and len(raw_groups) > 10:
        sections["repeated_sibling_candidates"] = True
    if groups and sum(len(group.get("examples", [])) for group in groups) < sum(
        min(len(item.get("examples", [])), 4) for item in raw_groups[:10] if isinstance(item, dict)
    ):
        sections["repeated_sibling_candidates"] = True
    if isinstance(raw_truncation, dict) and raw_truncation.get("truncated") is True:
        pass
    if raw_body.get("scanTruncated") is True:
        sections["body"] = True
    section_reasons = {
        "body": "dom-scan-limit", "candidate_containers": "container-sample-limit",
        "candidate_messages": "message-sample-limit", "interactive_elements": "interactive-sample-limit",
        "textarea_contexts": "textarea-sample-limit", "deal_links": "deal-link-sample-limit",
        "repeated_sibling_candidates": "repeat-group-limit", "composer_scopes": "composer-control-sample-limit",
        "dynamic_areas": "dynamic-area-sample-limit",
        "links": "link-count-limit",
    }
    for section, reason in section_reasons.items():
        if sections.get(section) is True and reason not in truncation_reasons:
            truncation_reasons.append(reason)
    result = {
        "schema": "playerok-dom-structure-v1",
        "status": "ok",
        "page": {"site": "playerok"},
        "body": {
            "element_count_scanned": _dom_bounded_int(raw_body.get("elementCount"), 0, 12000),
            "max_depth_scanned": _dom_bounded_int(raw_body.get("maxDepth"), 0, 12000),
            "scan_truncated": raw_body.get("scanTruncated") is True,
        },
        "counts": counts,
        "candidate_containers": nodes("candidateContainers", 40, "candidate_containers"),
        "candidate_messages": nodes("candidateMessages", 100, "candidate_messages"),
        "interactive_elements": nodes("interactiveElements", 100, "interactive_elements"),
        "textarea_contexts": textarea_contexts,
        "composer_scopes": composer_scopes,
        "repeated_sibling_candidates": groups,
        "dynamic_areas": nodes("dynamicAreas", 40, "dynamic_areas"),
        "link_count_exact": payload.get("linkCountExact") is True and not sections.get("links", False),
        "truncated": {
            "occurred": bool(truncation_reasons or any(sections.values())),
            "reasons": list(dict.fromkeys(truncation_reasons)),
            "sections": sections,
        },
    }
    return _fit_dom_summary(result)


def playerok_profile_root() -> Path:
    return Path.home() / ".gamedelivery" / "playerok-web-profile"


def playerok_profile_routes(username: str) -> dict[str, str]:
    if not isinstance(username, str) or not username.strip():
        raise ValueError("username текущего профиля Playerok недоступен.")
    seller = username.strip()
    if seller in {".", ".."}:
        raise ValueError("username текущего профиля Playerok некорректен.")
    seller = quote(seller, safe="")
    return {
        "Мои объявления": f"/profile/{seller}/products",
        "Продажи: Оплаченные (PAID)": f"/profile/{seller}/sales?status=paid",
        "Продажи: Отправленные (SENT)": f"/profile/{seller}/sales?status=sent",
        "Продажи: Завершённые (CONFIRMED)": f"/profile/{seller}/sales?status=confirmed",
        "Продажи: Возврат средств": f"/profile/{seller}/sales?status=rolled-back",
}


def playerok_resource_url(resource: str, identifier: str) -> str:
    value = str(identifier or "").strip()
    if not value or "/" in value or "\\" in value or value in {".", ".."}:
        raise ValueError("Некорректный идентификатор Playerok.")
    if resource == "deal":
        return f"{PLAYEROK_ORIGIN}/deal/{value}"
    if resource == "chat":
        return f"{PLAYEROK_ORIGIN}/chats/{value}"
    raise ValueError("Неизвестный тип ресурса Playerok.")


class _PlayerokDevToolsDialog(QDialog):
    def __init__(self, parent, closed_callback):
        super().__init__(parent, Qt.WindowType.Window)
        self._closed_callback = closed_callback

    def closeEvent(self, event) -> None:
        callback = self._closed_callback
        self._closed_callback = None
        if callback is not None:
            callback(self)
        event.accept()


class PlayerokBrowserController:
    """Small navigation controller, independently testable without WebEngine."""

    def __init__(self, navigate):
        self._navigate = navigate

    def open_deal(self, deal_id: str) -> str:
        url = playerok_resource_url("deal", deal_id)
        self._navigate(url)
        return url

    def open_chat(self, chat_id: str) -> str:
        url = playerok_resource_url("chat", chat_id)
        self._navigate(url)
        return url

    def open_profile_route(self, username: str, route_label: str) -> str:
        path = playerok_profile_routes(username).get(route_label)
        if path is None:
            raise ValueError("Неизвестный быстрый маршрут профиля Playerok.")
        url = PLAYEROK_ORIGIN + path
        self._navigate(url)
        return url


class PlayerokBrowserPanel(QWidget):
    listing_config_saved = Signal(object)

    def __init__(self, *, database=None, parent=None, enable_webengine: bool | None = None):
        super().__init__(parent)
        self.database = database
        self.controller = PlayerokBrowserController(self.navigate)
        self._profile = None
        self._devtools_window = None
        self._devtools_view = None
        self._devtools_page = None
        self._devtools_target_page_ref = None
        self._devtools_destroy_slot = None
        self._clear_pending = False
        self._page_storage_clear_ok = False
        self._page_ready = False
        self._dom_diagnostic_active = False
        self._viewer_checker = None
        self._profile_route_buttons: dict[str, QPushButton] = {}
        self._viewer_check_timeout = QTimer(self)
        self._viewer_check_timeout.setSingleShot(True)
        self._viewer_check_timeout.setInterval(30_000)
        self._viewer_check_timeout.timeout.connect(self._viewer_check_timed_out)
        root = QVBoxLayout(self)
        nav = QHBoxLayout()
        self.back_button = QPushButton("Назад")
        self.forward_button = QPushButton("Вперёд")
        self.reload_button = QPushButton("Обновить")
        self.home_button = QPushButton("Домой")
        self.devtools_button = QPushButton("Открыть DevTools")
        self.dom_diagnostic_button = QPushButton("Структура DOM (read-only)")
        self.graphql_context_button = QPushButton("Сохранить GraphQL-сессию")
        self.clear_session_button = QPushButton("Очистить данные сайта")
        self.devtools_button.clicked.connect(self.open_devtools)
        self.dom_diagnostic_button.clicked.connect(self._run_dom_diagnostic)
        self.graphql_context_button.clicked.connect(self.save_graphql_session_context)
        for button in (
            self.back_button, self.forward_button, self.reload_button,
            self.home_button, self.devtools_button, self.clear_session_button,
        ):
            nav.addWidget(button)
        self.address = QLineEdit(PLAYEROK_ORIGIN)
        self.go_button = QPushButton("Перейти")
        self.go_button.clicked.connect(lambda: self.navigate(self.address.text().strip()))
        nav.addWidget(self.address, 1)
        nav.addWidget(self.go_button)
        root.addLayout(nav)
        quick = QHBoxLayout()
        for label in PLAYEROK_PROFILE_ROUTE_LABELS:
            button = QPushButton(label)
            button.clicked.connect(
                lambda checked=False, route_label=label: self._open_profile_route(route_label)
            )
            self._profile_route_buttons[label] = button
            quick.addWidget(button)
        for label, path in PLAYEROK_FIXED_ROUTES.items():
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, p=path: self.navigate(PLAYEROK_ORIGIN + p))
            quick.addWidget(button)
        quick.addWidget(self.dom_diagnostic_button)
        quick.addWidget(self.graphql_context_button)
        root.addLayout(quick)

        split = QSplitter()
        self.browser_status = QLabel()
        headless_platform = os.environ.get("QT_QPA_PLATFORM", "").strip().lower() in {"offscreen", "minimal", "minimalegl"}
        use_webengine = (
            WEBENGINE_AVAILABLE and not headless_platform
            if enable_webengine is None
            else (WEBENGINE_AVAILABLE and enable_webengine)
        )
        if use_webengine:
            self.web = QWebEngineView()
            profile_dir = playerok_profile_root()
            profile_dir.mkdir(parents=True, exist_ok=True)
            self._profile = QWebEngineProfile("GameDelivery-Playerok", self)
            self._profile.setPersistentStoragePath(str(profile_dir / "storage"))
            self._profile.setCachePath(str(profile_dir / "cache"))
            self._profile.setPersistentCookiesPolicy(
                QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
            )
            page = QWebEnginePage(self._profile, self.web)
            self.web.setPage(page)
            self.web.urlChanged.connect(lambda u: self.address.setText(u.toString()))
            self.web.loadStarted.connect(lambda: self.browser_status.setText("Загрузка Playerok…"))
            self.web.loadFinished.connect(self._load_finished)
            self.back_button.clicked.connect(self.web.back)
            self.forward_button.clicked.connect(self.web.forward)
            self.reload_button.clicked.connect(self.web.reload)
            self.home_button.clicked.connect(lambda: self.navigate(PLAYEROK_ORIGIN + "/"))
            self.clear_session_button.clicked.connect(self.clear_browser_session)
            split.addWidget(self.web)
            self.browser_status.setText("Войдите на сайте Playerok. Сессия хранится локально в профиле браузера.")
            self.navigate(PLAYEROK_ORIGIN + "/")
        else:
            self.web = None
            self.graphql_context_button.setEnabled(False)
            for button in (self.back_button, self.forward_button, self.reload_button):
                button.setEnabled(False)
            self.home_button.clicked.connect(lambda: self.navigate(PLAYEROK_ORIGIN + "/"))
            self.clear_session_button.setEnabled(False)
            self.browser_status.setText(
                "QtWebEngine недоступен (headless/CI). Контроллер навигации и панель настроек доступны."
            )
        # Keep asynchronous browser/session diagnostics visible in both real
        # WebEngine and headless layouts. Previously the label was only added
        # in the headless branch, so capture results were invisible in normal UI.
        root.addWidget(self.browser_status)

        editor = QWidget()
        form = QVBoxLayout(editor)
        form.addWidget(QLabel("Привязка объявления → ConfigVersion"))
        self.listing_id = QLineEdit()
        self.listing_id.setPlaceholderText("listing_id Playerok")
        self.listing_title = QLineEdit()
        self.listing_title.setPlaceholderText("Название объявления")
        self.listing_url = QLineEdit()
        self.listing_url.setPlaceholderText("URL объявления")
        for field in (self.listing_id, self.listing_title, self.listing_url):
            form.addWidget(field)
        capture = QPushButton("Взять URL и данные из открытой страницы")
        capture.clicked.connect(self._capture_current_listing)
        form.addWidget(capture)
        self.mode = QComboBox()
        self.mode.addItem("Коллекционные карточки Steam (753/6)", "steam_cards")
        self.mode.addItem("Предметы CS2 (730/2)", "cs2_items")
        self.mode.addItem("Без Steam-обмена", "manual")
        self.mode.currentIndexChanged.connect(self._mode_changed)
        form.addWidget(self.mode)
        self.quantity = QSpinBox()
        self.quantity.setRange(1, 9999)
        self.quantity.setPrefix("Количество за покупку: ")
        form.addWidget(self.quantity)
        self.listing_stock_quantity = QSpinBox()
        self.listing_stock_quantity.setRange(1, 9999)
        self.listing_stock_quantity.setPrefix("Количество в объявлении: ")
        form.addWidget(self.listing_stock_quantity)
        self.bots = QLineEdit()
        self.bots.setPlaceholderText("Пул ASF-ботов (пусто = все найденные)")
        form.addWidget(self.bots)
        self.selectors = QPlainTextEdit()
        self.selectors.setPlaceholderText('CS2 selectors JSON, например: [{"market_hash_name":"Кейс"}]')
        form.addWidget(self.selectors, 1)
        self.messages = QPlainTextEdit()
        self.messages.setPlaceholderText('Автосообщения JSON: {"paid":"...","sent":"...","confirmed":"..."}')
        form.addWidget(self.messages, 1)
        self.save_button = QPushButton("Сохранить новую версию ConfigVersion")
        self.save_button.clicked.connect(self._save_config)
        form.addWidget(self.save_button)
        split.addWidget(editor)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        root.addWidget(split, 1)
        self._mode_changed()

    def navigate(self, url: str) -> None:
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname not in {"playerok.com", "www.playerok.com"}:
            raise ValueError("Встроенный браузер разрешает только HTTPS Playerok.")
        if self.web is not None:
            self.web.setUrl(QUrl(url))
        self.address.setText(url)

    def open_devtools(self) -> bool:
        """Open Chromium DevTools for the existing Playerok page, without navigation."""
        if self.web is None or self._profile is None or self._clear_pending:
            self.browser_status.setText(
                "Не удалось открыть DevTools: страница Playerok сейчас недоступна."
            )
            return False

        try:
            page = self.web.page()
            if page is None or not shiboken6.isValid(page):
                raise RuntimeError("invalid page")
        except (RuntimeError, TypeError, AttributeError):
            self.browser_status.setText(
                "Не удалось открыть DevTools: страница Playerok сейчас недоступна."
            )
            return False

        try:
            page_url = urlsplit(page.url().toString())
        except (RuntimeError, TypeError, AttributeError):
            page_url = None
        if (
            page_url is None
            or page_url.scheme != "https"
            or page_url.hostname not in {"playerok.com", "www.playerok.com"}
        ):
            self.browser_status.setText(
                "Не удалось открыть DevTools: сначала откройте Playerok во встроенном браузере."
            )
            return False

        current_target_ref = self._devtools_target_page_ref
        if current_target_ref is not None and current_target_ref() is page:
            try:
                attached_page = page.devToolsPage()
                if (
                    attached_page is self._devtools_page
                    and self._devtools_window is not None
                    and shiboken6.isValid(self._devtools_window)
                ):
                    self._activate_devtools_window()
                    return True
            except (RuntimeError, TypeError, AttributeError):
                pass
            self._discard_devtools_references(delete_window=True)
        elif self._devtools_window is not None:
            self._discard_devtools_references(delete_window=True)

        try:
            attached_page = page.devToolsPage()
        except (RuntimeError, TypeError, AttributeError):
            self.browser_status.setText(
                "Не удалось открыть DevTools: состояние страницы Playerok недоступно."
            )
            return False
        if attached_page is not None:
            self.browser_status.setText(
                "Не удалось открыть DevTools: к странице Playerok уже подключён другой инспектор."
            )
            return False

        window = None
        try:
            window = _PlayerokDevToolsDialog(self, self._devtools_window_closed)
            window.setWindowTitle("Chrome DevTools — Playerok")
            window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
            view = QWebEngineView(window)
            devtools_page = QWebEnginePage(self._profile, view)
            view.setPage(devtools_page)
            layout = QVBoxLayout(window)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(view)

            target_page_ref = weakref.ref(page)
            destroy_slot = lambda *_args, ref=target_page_ref: self._inspected_page_destroyed(ref)
            page.destroyed.connect(destroy_slot)

            self._devtools_window = window
            self._devtools_view = view
            self._devtools_page = devtools_page
            self._devtools_target_page_ref = target_page_ref
            self._devtools_destroy_slot = destroy_slot

            page.setDevToolsPage(devtools_page)
            window.resize(1100, 750)
            self._activate_devtools_window()
            self.browser_status.setText(
                "Chrome DevTools подключён к текущей странице Playerok."
            )
            return True
        except Exception:
            if self._devtools_window is window and window is not None:
                target_ref = self._devtools_target_page_ref
                target_page = target_ref() if target_ref is not None else None
                try:
                    if (
                        target_page is not None
                        and shiboken6.isValid(target_page)
                        and target_page.devToolsPage() is self._devtools_page
                    ):
                        target_page.setDevToolsPage(None)
                except (RuntimeError, TypeError, AttributeError):
                    pass
                self._discard_devtools_references(delete_window=True)
            elif window is not None:
                try:
                    window.close()
                    window.deleteLater()
                except (RuntimeError, AttributeError):
                    pass
            self.browser_status.setText(
                "Не удалось подключить DevTools к странице Playerok."
            )
            return False

    def _activate_devtools_window(self) -> None:
        window = self._devtools_window
        if window is None or not shiboken6.isValid(window):
            return
        window.show()
        window.raise_()
        window.activateWindow()

    def _inspected_page_destroyed(self, target_page_ref) -> None:
        if self._devtools_target_page_ref is not target_page_ref:
            return
        self._discard_devtools_references(delete_window=True)
        if shiboken6.isValid(self):
            self.browser_status.setText(
                "Страница Playerok закрыта; окно DevTools закрыто."
            )

    def _devtools_window_closed(self, window) -> None:
        if not shiboken6.isValid(self) or self._devtools_window is not window:
            return
        self._discard_devtools_references(delete_window=True, close_window=False)

    def _discard_devtools_references(
        self, *, delete_window: bool, close_window: bool = True
    ) -> None:
        window = self._devtools_window
        target_ref = self._devtools_target_page_ref
        destroy_slot = self._devtools_destroy_slot
        devtools_page = self._devtools_page
        self._devtools_window = None
        self._devtools_view = None
        self._devtools_page = None
        self._devtools_target_page_ref = None
        self._devtools_destroy_slot = None

        target_page = target_ref() if target_ref is not None else None
        if target_page is not None and destroy_slot is not None:
            try:
                if (
                    shiboken6.isValid(target_page)
                    and target_page.devToolsPage() is devtools_page
                ):
                    target_page.setDevToolsPage(None)
            except (RuntimeError, TypeError):
                pass
            try:
                target_page.destroyed.disconnect(destroy_slot)
            except (RuntimeError, TypeError):
                pass
        if window is not None:
            try:
                if close_window:
                    window.close()
                if delete_window:
                    window.deleteLater()
            except (RuntimeError, AttributeError):
                pass

    def open_deal(self, deal_id: str) -> str:
        return self.controller.open_deal(deal_id)

    def open_chat(self, chat_id: str) -> str:
        return self.controller.open_chat(chat_id)

    def _open_profile_route(self, route_label: str) -> None:
        self.browser_status.setText("Username профиля не получен из браузерной страницы. Откройте нужный раздел через сайт.")

    def _load_finished(self, ok: bool) -> None:
        self._page_ready = bool(ok)
        self.browser_status.setText(
            "Браузер открыт; статус входа Playerok не проверен."
            if ok else "Не удалось загрузить страницу Playerok."
        )

    @property
    def viewer_check_active(self) -> bool:
        return bool(self._viewer_checker is not None and self._viewer_checker.active)

    def check_authentication(self, finished) -> BrowserViewerChecker | None:
        """Check the current page session with one read-only viewer GET; do not sync sales."""
        if not callable(finished):
            raise TypeError("finished должен быть вызываемым")
        if self._viewer_checker is not None and self._viewer_checker.active:
            finished(False, "poll_state_error: проверка авторизации уже выполняется.")
            return None
        if self.web is None or not self._page_ready or self._clear_pending:
            message = "javascript_start_error: страница Playerok не готова для проверки авторизации."
            self.browser_status.setText(message)
            finished(False, message)
            return None

        page = self.web.page()
        current_url = urlsplit(page.url().toString())
        if current_url.scheme != "https" or current_url.hostname not in {"playerok.com", "www.playerok.com"}:
            message = "javascript_start_error: откройте Playerok во встроенном браузере."
            self.browser_status.setText(message)
            finished(False, message)
            return None

        checker_ref = None
        page_destroyed_state = [False]

        def page_destroyed(*_args):
            page_destroyed_state[0] = True
            checker = checker_ref[0] if checker_ref else None
            if checker is not None:
                checker.cancel(stage="page_destroyed")

        def page_is_alive() -> bool:
            return not page_destroyed_state[0] and shiboken6.isValid(page)

        def checker_finished(authorized: bool, error: str | None):
            self._viewer_check_timeout.stop()
            try:
                page.destroyed.disconnect(page_destroyed)
            except (RuntimeError, TypeError):
                pass
            checker = checker_ref[0] if checker_ref else None
            if self._viewer_checker is checker:
                self._viewer_checker = None
            if authorized:
                self.browser_status.setText("Авторизация Playerok подтверждена.")
            else:
                self.browser_status.setText(error or "viewer_parse_error: авторизация не подтверждена.")
            finished(authorized, error)

        checker = BrowserViewerChecker(
            page.runJavaScript,
            checker_finished,
            schedule=lambda callback: QTimer.singleShot(50, callback),
            is_page_alive=page_is_alive,
        )
        checker_ref = [checker]
        self._viewer_checker = checker
        page.destroyed.connect(page_destroyed)
        self._viewer_check_timeout.start()
        checker.start()
        return checker

    def _viewer_check_timed_out(self) -> None:
        if self._viewer_checker is not None:
            self._viewer_checker.cancel(stage="timeout")

    def read_page_hostname(self, finished) -> None:
        """Read only location.hostname from the existing loaded WebEngine page."""
        if not callable(finished):
            raise TypeError("finished должен быть вызываемым")
        if self.web is None or not self._page_ready or self._clear_pending:
            finished(None)
            return

        page = self.web.page()
        completed = [False]

        def complete(value=None):
            if completed[0]:
                return
            completed[0] = True
            try:
                page.destroyed.disconnect(page_destroyed)
            except (AttributeError, RuntimeError, TypeError):
                pass
            finished(value if type(value) is str else None)

        def page_destroyed(*_args):
            complete(None)

        try:
            page.destroyed.connect(page_destroyed)
        except (AttributeError, RuntimeError, TypeError):
            pass
        try:
            page.runJavaScript("location.hostname", complete)
        except Exception:
            complete(None)

    def _run_current_page_javascript(self, script: str, finished) -> bool:
        """Run a caller-supplied local script on the current authenticated Playerok page."""
        if not callable(finished):
            raise TypeError("finished должен быть вызываемым")
        if type(script) is not str or not script or len(script) > 1_000_000:
            finished(None)
            return False
        if self.web is None or not self._page_ready or self._clear_pending:
            finished(None)
            return False
        try:
            page = self.web.page()
            if page is None or not shiboken6.isValid(page):
                finished(None)
                return False
            current_url = urlsplit(page.url().toString())
            if current_url.scheme != "https" or current_url.hostname not in {"playerok.com", "www.playerok.com"}:
                finished(None)
                return False
        except (RuntimeError, TypeError, AttributeError):
            finished(None)
            return False
        completed = [False]

        def complete(value=None):
            if completed[0]:
                return
            completed[0] = True
            try:
                page.destroyed.disconnect(on_destroyed)
            except (RuntimeError, TypeError, AttributeError):
                pass
            if not page_is_current():
                finished(None)
                return
            finished(value)

        def page_is_current() -> bool:
            if self._clear_pending or self.web is None:
                return False
            try:
                return shiboken6.isValid(page) and self.web.page() is page
            except (RuntimeError, TypeError, AttributeError):
                return False

        def on_destroyed(*_args):
            complete(None)

        try:
            page.destroyed.connect(on_destroyed)
            page.runJavaScript(script, complete)
        except Exception:
            complete(None)
            return False
        return True

    def read_dom_structure(self, finished) -> bool:
        """Read a bounded, allowlisted structure summary from the current page only."""
        if not callable(finished):
            raise TypeError("finished должен быть вызываемым")

        def reject(message: str) -> bool:
            self.browser_status.setText(message)
            finished(None, message)
            return False

        if self.web is None or not self._page_ready or self._clear_pending:
            return reject("DOM-диагностика недоступна: страница не готова или очищается.")
        if self._dom_diagnostic_active:
            return reject("DOM-диагностика уже выполняется.")
        try:
            page = self.web.page()
            if page is None or not shiboken6.isValid(page):
                return reject("DOM-диагностика недоступна: страница уничтожена.")
            parsed_url = urlsplit(page.url().toString())
        except (RuntimeError, TypeError, AttributeError):
            return reject("DOM-диагностика недоступна: состояние страницы не подтверждено.")
        if parsed_url.scheme != "https" or parsed_url.hostname not in {"playerok.com", "www.playerok.com"}:
            return reject("DOM-диагностика разрешена только на текущей странице Playerok.")

        completed = [False]
        page_destroyed = [False]

        def page_is_live() -> bool:
            if page_destroyed[0] or self._clear_pending or self.web is None:
                return False
            try:
                return shiboken6.isValid(page) and self.web.page() is page
            except (RuntimeError, TypeError, AttributeError):
                return False

        def complete(summary=None, error: str | None = None) -> None:
            if completed[0]:
                return
            completed[0] = True
            if not page_destroyed[0]:
                try:
                    if shiboken6.isValid(page):
                        page.destroyed.disconnect(on_page_destroyed)
                except (RuntimeError, TypeError, AttributeError):
                    pass
            try:
                if not shiboken6.isValid(self):
                    return
                self._dom_diagnostic_active = False
                self.browser_status.setText(
                    "Структура DOM прочитана; содержимое не сохранено."
                    if error is None else error
                )
                finished(summary, error)
            except (RuntimeError, TypeError, AttributeError):
                return

        def on_page_destroyed(*_args) -> None:
            page_destroyed[0] = True
            complete(None, "Страница браузера уничтожена до завершения DOM-диагностики.")

        def decode_result(raw_value: str) -> None:
            if completed[0]:
                return
            if not page_is_live():
                complete(None, "Страница браузера изменилась до получения результата DOM.")
                return
            try:
                summary = _sanitize_dom_summary(raw_value)
            except (TypeError, ValueError, RecursionError, UnicodeError):
                # Keep malformed or unexpectedly deep callback payloads inside the
                # diagnostic schema instead of letting the UI fall back to a generic rejection.
                summary = _dom_error_summary("serialization-error")
            complete(summary, None)

        def javascript_completed(value) -> None:
            if completed[0]:
                return
            if not page_is_live():
                complete(None, "Страница браузера уничтожена до получения результата DOM.")
                return
            if type(value) is not str:
                complete(_dom_error_summary("callback-result-not-string"), None)
                return
            if len(value) > _DOM_DIAGNOSTIC_CALLBACK_MAX_BYTES:
                complete(_dom_error_summary("callback-byte-limit"), None)
                return
            try:
                if len(value.encode("utf-8")) > _DOM_DIAGNOSTIC_CALLBACK_MAX_BYTES:
                    complete(_dom_error_summary("callback-byte-limit"), None)
                    return
            except UnicodeError:
                complete(_dom_error_summary("invalid-json"), None)
                return
            # JSON decoding is deferred out of the WebEngine callback and remains bounded.
            QTimer.singleShot(0, lambda raw=value: decode_result(raw))

        try:
            page.destroyed.connect(on_page_destroyed)
        except (RuntimeError, TypeError, AttributeError):
            return reject("DOM-диагностика не смогла отслеживать жизненный цикл страницы.")

        self._dom_diagnostic_active = True
        try:
            page.runJavaScript(_DOM_DIAGNOSTIC_SCRIPT, javascript_completed)
        except Exception:
            complete(_dom_error_summary("javascript-evaluation-error"), None)
            return False
        return True

    def _run_dom_diagnostic(self) -> None:
        self.dom_diagnostic_button.setEnabled(False)

        def completed(summary, error) -> None:
            try:
                if not shiboken6.isValid(self):
                    return
                self.dom_diagnostic_button.setEnabled(True)
            except (RuntimeError, TypeError, AttributeError):
                return
            if error is not None:
                self._show_dom_diagnostic_text(error)
                return
            self._show_dom_diagnostic_text(json.dumps(summary, ensure_ascii=False, indent=2))

        self.read_dom_structure(completed)

    def _show_dom_diagnostic_text(self, text: str) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Структура DOM текущей страницы Playerok")
        dialog.resize(900, 700)
        layout = QVBoxLayout(dialog)
        output = QPlainTextEdit(dialog)
        output.setReadOnly(True)
        output.setPlainText(text)
        layout.addWidget(output)
        close_button = QPushButton("Закрыть", dialog)
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button)
        dialog.exec()

    def read_sales(self, finished, *, on_request_started=None) -> BrowserSalesReader:
        if self.web is None or not self._page_ready or self._clear_pending:
            raise RuntimeError("Сначала дождитесь загрузки сайта Playerok во встроенном браузере.")
        page = self.web.page()
        current_url = urlsplit(page.url().toString())
        if current_url.scheme != "https" or current_url.hostname not in {"playerok.com", "www.playerok.com"}:
            raise RuntimeError("Откройте Playerok во встроенном браузере перед синхронизацией продаж.")
        reader_ref = None

        def page_destroyed(*_args):
            current_reader = reader_ref() if reader_ref is not None else None
            if current_reader is not None:
                current_reader.cancel(stage="page_destroyed")

        def reader_finished(snapshot, error):
            try:
                page.destroyed.disconnect(page_destroyed)
            except (RuntimeError, TypeError):
                pass
            finished(snapshot, error)

        reader = BrowserSalesReader(
            page.runJavaScript,
            reader_finished,
            schedule=lambda callback: QTimer.singleShot(50, callback),
            on_request_started=on_request_started,
        )
        reader_ref = weakref.ref(reader)
        page.destroyed.connect(page_destroyed)
        reader.start()
        return reader

    def read_graphql_sales_snapshot(
        self, finished, *, expected_account_user_id: str | None = None
    ) -> PlayerokBrowserGraphQLSalesReader:
        """Read a complete GraphQL snapshot using this page's browser session."""
        if not callable(finished):
            raise TypeError("finished должен быть вызываемым")
        if not self.sales_session_available():
            raise RuntimeError("browser_session_unavailable")
        page = self.web.page()
        try:
            if page is None or not shiboken6.isValid(page) or page.profile() != self._profile:
                raise RuntimeError("browser_profile_unavailable")
        except (RuntimeError, TypeError, AttributeError) as exc:
            raise RuntimeError("browser_profile_unavailable") from None

        transport = WebEngineGraphQLTransport(
            self._run_current_page_javascript,
            schedule=lambda callback: QTimer.singleShot(50, callback),
        )
        reader_ref: list[PlayerokBrowserGraphQLSalesReader | None] = [None]

        def page_destroyed(*_args):
            reader = reader_ref[0]
            if reader is not None:
                reader.cancel(stage="page_destroyed")

        def completed(snapshot, error):
            try:
                page.destroyed.disconnect(page_destroyed)
            except (RuntimeError, TypeError):
                pass
            finished(snapshot, error)

        reader = PlayerokBrowserGraphQLSalesReader(
            transport, completed,
            expected_account_user_id=expected_account_user_id,
        )
        reader_ref[0] = reader
        page.destroyed.connect(page_destroyed)
        reader.start()
        return reader

    def sales_session_available(self) -> bool:
        """Whether the current WebEngine page is ready for a read-only sales pass."""
        if self.web is None or not self._page_ready or self._clear_pending:
            return False
        try:
            page = self.web.page()
            if page is None or not shiboken6.isValid(page):
                return False
            current_url = urlsplit(page.url().toString())
        except (RuntimeError, TypeError, AttributeError):
            return False
        return (
            current_url.scheme == "https"
            and current_url.hostname in {"playerok.com", "www.playerok.com"}
        )

    def save_graphql_session_context(self, _checked=False, finished=None) -> bool:
        """Persist only allowlisted cookies from the current profile into keyring."""
        if finished is not None and not callable(finished):
            raise TypeError("finished должен быть вызываемым")
        if not self.sales_session_available() or self._profile is None:
            self.browser_status.setText(
                "Контекст GraphQL неполный: страница или профиль WebEngine Playerok не готовы."
            )
            if callable(finished):
                finished(False, "page_unavailable")
            return False
        if getattr(self, "_graphql_context_capture", None) is not None:
            self.browser_status.setText(
                "Ошибка сохранения GraphQL-контекста: capture_in_progress."
            )
            if callable(finished):
                finished(False, "capture_in_progress")
            return False

        # Do not capture from a profile other than the one backing the page the
        # operator is currently using.  This also makes an unexpected page
        # replacement fail closed instead of silently reading another store.
        try:
            page = self.web.page()
            if page is None or not shiboken6.isValid(page):
                raise RuntimeError
            page_profile = page.profile()
            same_profile = page_profile is self._profile or page_profile == self._profile
        except (RuntimeError, TypeError, AttributeError):
            same_profile = False
        if not same_profile:
            self.browser_status.setText(
                "Контекст GraphQL неполный: текущая страница использует другой профиль WebEngine."
            )
            if callable(finished):
                finished(False, "profile_mismatch")
            return False

        cookie_store = self._profile.cookieStore()
        quiet_timer = QTimer(self)
        quiet_timer.setSingleShot(True)
        quiet_timer.setInterval(350)
        hard_timer = QTimer(self)
        hard_timer.setSingleShot(True)
        hard_timer.setInterval(4000)
        state = {"cookies": {}, "cookie_metadata": [], "finished": False}
        self._graphql_context_capture = state

        def decode(value):
            if type(value) is str:
                return value
            try:
                return bytes(value).decode("utf-8")
            except (TypeError, UnicodeDecodeError):
                return ""

        def cookie_added(cookie):
            try:
                domain = decode(cookie.domain()).lstrip(".").lower()
                name = decode(cookie.name())
            except (AttributeError, RuntimeError, TypeError):
                return
            allowed_domain = domain == "playerok.com" or domain.endswith(".playerok.com")
            if allowed_domain:
                def safe_metadata_text(raw, limit):
                    text = decode(raw)
                    if not text or any(ch in text for ch in "\r\n\x00"):
                        return None
                    return text[:limit]

                try:
                    expiry = cookie.expirationDate()
                    expiry_utc = (
                        expiry.toUTC().toString(Qt.DateFormat.ISODate)
                        if expiry is not None and expiry.isValid()
                        else None
                    )
                    metadata = {
                        "name": safe_metadata_text(cookie.name(), 80),
                        "domain": safe_metadata_text(cookie.domain(), 120),
                        "path": safe_metadata_text(cookie.path(), 160),
                        "secure": bool(cookie.isSecure()),
                        "http_only": bool(cookie.isHttpOnly()),
                        "session": bool(cookie.isSessionCookie()),
                        "expires_utc": expiry_utc[:40] if expiry_utc else None,
                        "cookie_type": type(cookie).__name__[:40],
                    }
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    metadata = {"name": safe_metadata_text(cookie.name(), 80), "metadata_error": True}
                # Keep a small bounded, value-free view for diagnosing why the
                # required token cookie was not present in this profile.
                if len(state["cookie_metadata"]) < 16 and metadata not in state["cookie_metadata"]:
                    state["cookie_metadata"].append(metadata)
            allowed_name = name in {"token", "__ddg5_", "__ddg3", "cf_clearance", "__cf_bm"}
            if not allowed_domain:
                return
            if allowed_name:
                try:
                    value = decode(cookie.value())
                except (AttributeError, RuntimeError, TypeError):
                    value = ""
                if value and len(value) <= 4096:
                    state["cookies"][name] = value
            # loadAllCookies() has no finished signal. Do not start the quiet
            # window before the first Playerok cookie arrives: that used to
            # fail an otherwise valid capture 350 ms after the request began.
            quiet_timer.start()

        def finish(outcome, reason=None):
            if state["finished"]:
                return
            state["finished"] = True
            quiet_timer.stop()
            hard_timer.stop()
            try:
                cookie_store.cookieAdded.disconnect(cookie_added)
            except (RuntimeError, TypeError):
                pass
            saved = False
            if outcome == "save":
                if "token" not in state["cookies"]:
                    outcome, reason = "incomplete", "session_cookie_missing"
                else:
                    try:
                        from integrations.playerok.graphql_http import (
                            PlayerokGraphQLTransportError,
                            PlayerokReadOnlyGraphQLClient,
                        )
                        PlayerokReadOnlyGraphQLClient.save_browser_context(
                            state["cookies"], self._profile.httpUserAgent()
                        )
                        saved = True
                    except PlayerokGraphQLTransportError as exc:
                        outcome, reason = "error", exc.code
                    except Exception as exc:
                        # Exception text may contain sensitive data; expose only
                        # its safe class name for unexpected local failures.
                        outcome, reason = "error", f"unexpected_{type(exc).__name__}"
            self._graphql_context_capture = None
            quiet_timer.deleteLater()
            hard_timer.deleteLater()
            if not self._clear_pending:
                self.graphql_context_button.setEnabled(True)
                self.clear_session_button.setEnabled(True)
            if saved:
                self.browser_status.setText(
                    "GraphQL-контекст сохранён и подтверждён чтением из keyring. "
                    "Действительность сессии отдельно проверяется запросом viewer."
                )
            elif outcome == "incomplete":
                reason_text = {
                    "session_cookie_missing": "cookie сессии Playerok не получена",
                    "cookie_store_timeout": "чтение cookies не завершилось в отведённое время",
                }.get(reason, reason or "неизвестная причина")
                diagnosis = ""
                if reason == "session_cookie_missing":
                    page_host = "unknown"
                    try:
                        page_host = urlsplit(self.web.page().url().toString()).hostname or "unknown"
                    except (RuntimeError, TypeError, AttributeError):
                        pass
                    observed = state["cookie_metadata"]
                    if observed:
                        records = []
                        for item in observed[:6]:
                            if item.get("metadata_error"):
                                records.append(f"name={item.get('name') or 'unnamed'} metadata=unavailable")
                                continue
                            records.append(
                                "name={name} domain={domain} path={path} secure={secure} "
                                "http_only={http_only} session={session} expires_utc={expires} type={cookie_type}".format(
                                    name=item.get("name") or "unnamed",
                                    domain=item.get("domain") or "unknown",
                                    path=item.get("path") or "unknown",
                                    secure=int(item["secure"]),
                                    http_only=int(item["http_only"]),
                                    session=int(item["session"]),
                                    expires=item.get("expires_utc") or "session-cookie",
                                    cookie_type=item.get("cookie_type") or "unknown",
                                )
                            )
                        diagnosis = (
                            f" Страница: {page_host}; first-party cookie metadata: ["
                            + "; ".join(records)
                            + (
                                f"; ещё {len(observed) - 6} не показано" if len(observed) > 6 else ""
                            )
                            + "]; значения cookies скрыты."
                        )
                    else:
                        diagnosis = (
                            f" Страница: {page_host}; cookieAdded не вернул cookie "
                            "домена Playerok за время capture."
                        )
                self.browser_status.setText(
                    f"Контекст GraphQL неполный: {reason_text}; ничего не сохранено.{diagnosis}"
                )
            else:
                reason_text = {
                    "keyring_write_failed": "Windows keyring отклонил запись",
                    "keyring_readback_failed": "не удалось прочитать запись обратно из keyring",
                    "keyring_readback_mismatch": "прочитанный контекст не совпал с записанным",
                    "invalid_session_context": "контекст не прошёл проверку полей",
                    "unsupported_session_fingerprint": "User-Agent не поддерживается transport",
                    "cookie_store_start_failed": "QtWebEngine не начал чтение cookie store",
                    "profile_mismatch": "текущая страница использует другой профиль WebEngine",
                }.get(reason, reason or "неизвестная причина")
                self.browser_status.setText(
                    f"Ошибка сохранения GraphQL-контекста: {reason_text}. Сохранение не подтверждено."
                )
            if callable(finished):
                try:
                    finished(saved, reason)
                except Exception:
                    pass

        def cookie_store_timeout():
            if "token" in state["cookies"]:
                finish("incomplete", "cookie_store_timeout")
            else:
                finish("incomplete", "session_cookie_missing")

        def quiet_period_finished():
            # A first-party cookie event without the required session cookie is
            # not proof the asynchronous enumeration is complete. Keep waiting
            # for a later token event or the hard deadline.
            if "token" not in state["cookies"]:
                return
            finish("save")

        quiet_timer.timeout.connect(quiet_period_finished)
        hard_timer.timeout.connect(cookie_store_timeout)
        self.graphql_context_button.setEnabled(False)
        self.clear_session_button.setEnabled(False)
        self.browser_status.setText("Ожидаю cookies текущего профиля Playerok…")
        try:
            cookie_store.cookieAdded.connect(cookie_added)
            cookie_store.loadAllCookies()
            hard_timer.start()
        except Exception:
            finish("error", "cookie_store_start_failed")
            return False
        return True

    def clear_browser_session(self) -> bool:
        if self._profile is None:
            self.browser_status.setText("Профиль WebEngine недоступен в этом режиме.")
            return False
        answer = QMessageBox.question(
            self, "Очистить сессию Playerok",
            "Очистить Web Storage и HTTP-кэш Playerok и запросить удаление cookies? "
            "Qt не сообщает о завершении удаления cookies, поэтому полное завершение выхода подтвердить нельзя.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self._keyring_context_clear_ok = True
        try:
            import keyring
            from integrations.playerok.client import KEYRING_ACCOUNT, KEYRING_SERVICE
            if keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT) is not None:
                keyring.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except Exception:
            self._keyring_context_clear_ok = False
        self._clear_pending = True
        self.clear_session_button.setEnabled(False)
        self.browser_status.setText("Очищаю Web Storage и HTTP-кэш; отправляю запрос на удаление cookies…")
        self._profile.clearHttpCacheCompleted.connect(self._finish_browser_clear)
        self.web.page().runJavaScript(
            "(() => { try { localStorage.clear(); sessionStorage.clear(); return true; } catch (_) { return false; } })()",
            self._page_storage_cleared,
        )
        return True

    def _page_storage_cleared(self, cleared: bool) -> None:
        if not self._clear_pending:
            return
        self._page_storage_clear_ok = bool(cleared)
        self._profile.cookieStore().deleteAllCookies()
        self._profile.clearHttpCache()

    def _finish_browser_clear(self) -> None:
        if not self._clear_pending:
            return
        self._profile.clearHttpCacheCompleted.disconnect(self._finish_browser_clear)
        self._clear_pending = False
        old_page = self.web.page()
        self.web.setPage(QWebEnginePage(self._profile, self.web))
        old_page.deleteLater()
        self.web.setUrl(QUrl("about:blank"))
        self.address.clear()
        self.clear_session_button.setEnabled(True)
        if not self._page_storage_clear_ok:
            self.browser_status.setText(
                "HTTP-кэш очищен; запрос на удаление cookies отправлен. Очистку Web Storage подтвердить не удалось, "
                "а удаление cookies и прочих persistent site data не подтверждено."
            )
        else:
            self.browser_status.setText(
                "Web Storage и HTTP-кэш очищены; запрос на удаление cookies отправлен. "
                "Qt не подтверждает завершение удаления cookies/прочих persistent site data, поэтому полный выход не подтверждён."
            )
        if not getattr(self, "_keyring_context_clear_ok", True):
            self.browser_status.setText(
                self.browser_status.text() + " Не удалось очистить GraphQL-сессию из системного хранилища."
            )

    def _capture_current_listing(self):
        current = self.address.text().strip()
        parsed = urlsplit(current)
        if parsed.scheme != "https" or parsed.hostname not in {"playerok.com", "www.playerok.com"}:
            self.browser_status.setText("Открой объявление Playerok во встроенном браузере.")
            return
        self.listing_url.setText(current)
        # A route slug or query parameter is not evidence of canonical item.id.
        # Clear any stale value so copying another page cannot silently bind it.
        self.listing_id.clear()
        if self.web is not None and self.web.title():
            self.listing_title.setText(self.web.title())
        self.browser_status.setText(
            "URL скопирован. Slug не используется как listing_id; введи подтверждённый canonical item.id отдельно."
        )

    def _mode_changed(self, *_):
        mode = self.mode.currentData()
        self.quantity.setVisible(mode != "manual")
        self.bots.setVisible(mode == "cs2_items")
        self.selectors.setVisible(mode == "cs2_items")
        self.listing_stock_quantity.setVisible(mode in {"cs2_items", "steam_cards"})
        self.listing_stock_quantity.setPrefix(
            "Наборов в объявлении: " if mode == "steam_cards" else "Количество в объявлении: "
        )
        self.messages.setVisible(mode == "manual")

    def _save_config(self):
        if self.database is None:
            QMessageBox.warning(self, "ConfigVersion", "Локальная база не передана панели.")
            return
        try:
            from integrations.playerok.listing_config import PlayerokListingConfigService
            mode = self.mode.currentData()
            selectors = json.loads(self.selectors.toPlainText() or "[]") if mode == "cs2_items" else None
            messages = json.loads(self.messages.toPlainText() or "{}") if mode == "manual" else None
            bots = [x.strip() for x in self.bots.text().split(",") if x.strip()]
            if mode == "cs2_items" and not bots:
                from app.bot_selector import configured_bot_names
                bots = configured_bot_names()
                if not bots:
                    raise ValueError("Не найдены ASF-боты. Настрой ASF или укажи пул ботов явно.")
            service = PlayerokListingConfigService(self.database)
            result = service.save(
                external_listing_id=self.listing_id.text(),
                title=self.listing_title.text(),
                url=self.listing_url.text(),
                mode=mode,
                quantity=self.quantity.value(),
                listing_quantity=self.listing_stock_quantity.value() if mode == "steam_cards" else None,
                bot_pool=bots,
                selectors=selectors,
                auto_messages=messages,
            )
            stock_summary = ""
            if mode == "steam_cards":
                stock_summary = (
                    f" Лимит объявления: {result['listing_quantity']} наборов. "
                    "Карты для каждого заказа подбираются автоматически после проверки покупателя."
                )
            if mode == "cs2_items":
                if result.get("recipe_items"):
                    try:
                        stock = service.allocate_listing_stock(
                            config_version_id=result["config_version_id"],
                            listing_quantity=self.listing_stock_quantity.value(),
                        )
                    except Exception as stock_exc:
                        self.listing_config_saved.emit(result)
                        self.browser_status.setText(
                            f"ConfigVersion #{result['version_number']} сохранена, "
                            "но общий склад не выделен. Заказы по этой версии будут заблокированы."
                        )
                        QMessageBox.warning(
                            self, "Общий склад",
                            f"ConfigVersion сохранена, но резервирование остатка не выполнено: {stock_exc}",
                        )
                        return
                    stock_summary = (
                        f" Общий склад: зарезервировано {stock['reserved_units']} шт. "
                        f"для {stock['listing_quantity']} продаж."
                    )
                else:
                    stock_summary = (
                        " Общий склад не выделен: несколько selectors трактуются как альтернативы; "
                        "для точного резерва нужен один однозначный CS2 selector."
                    )
            self.listing_config_saved.emit(result)
            self.browser_status.setText(
                f"Сохранена неизменяемая ConfigVersion #{result['version_number']}.{stock_summary}"
            )
        except Exception as exc:
            QMessageBox.warning(self, "ConfigVersion", str(exc))
