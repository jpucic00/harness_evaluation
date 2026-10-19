# Accessibility review format

Write `findings.json` as a JSON list. One object per violation:

```json
[
  {"file": "src/components/Example.jsx", "line": 12, "rule": "img-alt", "note": "why"}
]
```

- `file`: path relative to the repository root.
- `line`: the line of the offending element or declaration (its opening tag or
  property line).
- `rule`: exactly one id from the table below.
- `note`: free text.

Report each problem once. Correct code must not be reported.

| Rule id | Meaning |
| --- | --- |
| `img-alt` | An informative image has no text alternative. |
| `form-label` | A form control has no accessible label. |
| `button-name` | A button has no accessible name. |
| `link-name` | A link has no accessible name or its text does not describe its purpose. |
| `click-no-keyboard` | A non-interactive element handles clicks but cannot be focused or used with the keyboard. |
| `heading-order` | Heading levels skip a level. |
| `color-contrast` | Text contrast is below 4.5:1 (3:1 for large text). |
| `focus-visible` | The focus indicator is removed without a replacement. |
| `tabindex-positive` | A positive `tabIndex` changes the natural focus order. |
| `aria-hidden-focusable` | A focusable element is inside an `aria-hidden` container. |
| `html-lang` | The page does not declare its language. |
| `media-autoplay` | Media with sound plays automatically without a way to stop it. |
