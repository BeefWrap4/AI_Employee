import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import KnowledgeView from '../views/KnowledgeView.jsx'

// A fetch dispatcher that returns canned bodies by URL substring.
// The first /api/v1/documents GET returns an empty doc list; the
// POST /api/v1/documents returns a 202-created doc body.
function makeFetch() {
  return vi.fn().mockImplementation((url, init) => {
    const s = String(url)
    const method = (init && init.method) || 'GET'
    if (s.includes('/api/v1/documents') && method === 'POST') {
      return Promise.resolve({
        ok: true,
        status: 202,
        text: () =>
          Promise.resolve(
            JSON.stringify({
              doc_id: 'doc-new',
              title: 'upload-title',
              status: 'pending',
              mime_type: 'text/plain',
            }),
          ),
      })
    }
    if (s.includes('/api/v1/chat/query')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        text: () => Promise.resolve(JSON.stringify({ answer: 'noop', citations: [] })),
      })
    }
    // default: list documents
    return Promise.resolve({
      ok: true,
      status: 200,
      text: () => Promise.resolve(JSON.stringify({ items: [], total: 0 })),
    })
  })
}

describe('KnowledgeView document upload', () => {
  beforeEach(() => {
    global.fetch = makeFetch()
  })
  afterEach(() => vi.restoreAllMocks())

  it('renders an upload button and a title input', () => {
    render(<KnowledgeView />)
    expect(screen.getByRole('button', { name: /上传文档/ })).toBeInTheDocument()
    expect(screen.getByPlaceholderText('文档标题')).toBeInTheDocument()
  })

  it('POSTs a multipart FormData body to /api/v1/documents on upload', async () => {
    const fetchMock = global.fetch
    render(<KnowledgeView />)

    // Fill in the title.
    const titleInput = screen.getByPlaceholderText('文档标题')
    fireEvent.change(titleInput, { target: { value: 'upload-title' } })

    // antd Upload renders an <input type="file">. Find it and dispatch
    // a change event with a synthetic file so the beforeUpload handler
    // fires.
    const fileInput = document.querySelector('input[type="file"]')
    expect(fileInput).not.toBeNull()
    const file = new File(['hello world'], 'note.txt', { type: 'text/plain' })

    await act(async () => {
      fireEvent.change(fileInput, { target: { files: [file] } })
    })

    // The upload should have triggered a fetch POST with a FormData body.
    const postCall = await waitFor(() => {
      const found = fetchMock.mock.calls.find(
        ([url, init]) =>
          String(url).includes('/api/v1/documents') &&
          init &&
          init.method === 'POST' &&
          init.body instanceof FormData,
      )
      if (!found) throw new Error('no FormData POST observed')
      return found
    })

    const [, init] = postCall
    const form = init.body
    expect(form.get('title')).toBe('upload-title')
    expect(form.get('file')).toBe(file)
    expect(form.get('version')).toBe('v1')
    expect(JSON.parse(form.get('metadata_json'))).toEqual({})
    expect(JSON.parse(form.get('acl_tags_json'))).toEqual([])
    // Content-Type must NOT be application/json for a form upload; the
    // browser sets the multipart boundary, so the helper must omit it.
    expect(init.headers['Content-Type']).toBeUndefined()
  })
})
