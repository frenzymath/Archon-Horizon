declare module "@citation-js/core" {
  export class Cite {
    constructor(input: string, options?: { forceType?: string });
    data: Record<string, any>[];
  }
}

declare module "@citation-js/plugin-bibtex";
