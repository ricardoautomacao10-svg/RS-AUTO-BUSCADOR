const API_BASE = "http://localhost:8000";

export const NewsFeed = {
  list: async () => {
    const res = await fetch(`${API_BASE}/feeds`);
    if (!res.ok) throw new Error("Erro ao buscar feeds");
    return res.json();
  },
  create: async (data) => {
    const res = await fetch(`${API_BASE}/feeds`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    if (!res.ok) throw new Error("Erro ao criar feed");
    return res.json();
  },
  delete: async (id) => {
    const res = await fetch(`${API_BASE}/feeds/${id}`, {
      method: "DELETE",
    });
    if (!res.ok) throw new Error("Erro ao deletar feed");
  }
};
