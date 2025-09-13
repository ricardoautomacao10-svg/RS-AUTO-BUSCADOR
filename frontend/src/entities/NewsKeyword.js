const API_BASE = "http://localhost:8000";

export const NewsKeyword = {
  list: async () => {
    const res = await fetch(`${API_BASE}/keywords`);
    if (!res.ok) throw new Error("Erro ao buscar keywords");
    return res.json();
  },

  create: async (data) => {
    const res = await fetch(`${API_BASE}/keywords`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    if (!res.ok) throw new Error("Erro ao criar keyword");
    return res.json();
  },

  update: async (id, data) => {
    const res = await fetch(`${API_BASE}/keywords/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    if (!res.ok) throw new Error("Erro ao atualizar keyword");
    return res.json();
  },

  delete: async (id) => {
    const res = await fetch(`${API_BASE}/keywords/${id}`, { method: "DELETE" });
    if (!res.ok) throw new Error("Erro ao deletar keyword");
  }
};
