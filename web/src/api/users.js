import client from './client'

export function listUsers(params) {
  return client.get('/users', { params })
}

export function createUser(data) {
  return client.post('/users', data)
}

export function updateUserStatus(userId, body) {
  return client.put(`/users/${userId}/status`, body)
}
