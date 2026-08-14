import client from './client'

export function login(username, password) {
  return client.post('/auth/login', { username, password })
}

export function refresh(refreshToken) {
  return client.post('/auth/refresh', { refresh_token: refreshToken })
}
