import {registry} from "@web/core/registry";

registry.category("web_tour.tours").add("gq_job_graph_tour", {
    steps: () => [
        {
            content: "Abrir la pestaña del grafo",
            trigger: ".o_notebook .nav-link:contains(Graph)",
            run: "click",
        },
        {
            content: "El job actual y sus dos vecinos estan dibujados",
            trigger:
                ".o_gq_job_graph svg:has(g.o_gq_node_current):has(g.o_gq_node + g.o_gq_node + g.o_gq_node)",
        },
        {
            content: "Las dependencias tienen flecha",
            trigger: ".o_gq_job_graph svg:has(path.o_gq_edge + path.o_gq_edge)",
        },
        {
            content: "Clic en la dependencia abre su formulario",
            trigger: ".o_gq_job_graph g.o_gq_node:has(text:contains(GQ first))",
            run: "click",
        },
        {
            content: "Se abrio el job de la dependencia",
            trigger: ".o_field_widget[name=name]:contains(GQ first)",
        },
    ],
});
