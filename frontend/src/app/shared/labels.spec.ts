import { describe, expect, it } from 'vitest';
import { errorLabel, formatNumeric, statusLabel } from './labels';

describe('labels', () => {
  it('formats numeric envelopes without collapsing comparators or ranges', () => {
    expect(formatNumeric({ comparator: 'range', min: 10, max: 20, unit: '%' })).toBe('10 – 20 %');
    expect(formatNumeric({ comparator: 'lte', min: null, max: 5, unit: '%' })).toBe('≤ 5 %');
    expect(formatNumeric({ comparator: 'gt', min: 93, max: null, unit: null })).toBe('> 93');
    expect(formatNumeric(null)).toBe('');
  });

  it('maps statuses and error codes to Traditional Chinese', () => {
    expect(statusLabel('needs_review')).toBe('需人工核對');
    expect(errorLabel('needs_ocr')).toContain('OCR');
    expect(errorLabel('something_else')).toBe('something_else');
  });
});
