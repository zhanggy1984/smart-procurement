import client from './client'

export function listPendingConflicts(params) {
  return client.get('/pending-conflicts', { params })
}

export function importConflicts(file) {
  const fd = new FormData()
  fd.append('file', file)
  return client.post('/conflicts/import', fd)
}
