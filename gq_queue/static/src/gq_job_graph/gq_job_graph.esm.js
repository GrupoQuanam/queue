import {Component} from "@odoo/owl";
import {registry} from "@web/core/registry";
import {useService} from "@web/core/utils/hooks";
import {standardFieldProps} from "@web/views/fields/standard_field_props";

const NODE_WIDTH = 200;
const NODE_HEIGHT = 40;
const GAP_X = 60;
const GAP_Y = 16;
const PADDING = 12;
const MAX_LABEL = 26;

/**
 * Grafo de dependencias de un job: columnas por nivel (una dependencia queda
 * siempre a la izquierda de sus dependientes) y flechas de dependencia a
 * dependiente. Clic en un job abre su formulario.
 */
export class GQJobGraph extends Component {
    static template = "gq_queue.GQJobGraph";
    static props = {...standardFieldProps};

    setup() {
        this.action = useService("action");
    }

    get graph() {
        return this.props.record.data[this.props.name] || {nodes: [], edges: []};
    }

    get layout() {
        const {nodes, edges, current} = this.graph;
        const parents = new Map(nodes.map((node) => [node.id, []]));
        for (const [from, to] of edges) {
            parents.get(to).push(from);
        }
        // Nivel = camino mas largo desde un job sin dependencias
        const levels = new Map();
        const levelOf = (id) => {
            if (!levels.has(id)) {
                levels.set(id, 0);
                const ups = parents.get(id);
                levels.set(id, ups.length ? Math.max(...ups.map(levelOf)) + 1 : 0);
            }
            return levels.get(id);
        };
        const columns = [];
        for (const node of nodes) {
            const level = levelOf(node.id);
            (columns[level] ||= []).push(node);
        }
        const positions = new Map();
        const boxes = [];
        columns.forEach((column, level) => {
            column.forEach((node, row) => {
                const x = PADDING + level * (NODE_WIDTH + GAP_X);
                const y = PADDING + row * (NODE_HEIGHT + GAP_Y);
                positions.set(node.id, {x, y});
                const name = node.name || `#${node.id}`;
                boxes.push({
                    ...node,
                    x,
                    y,
                    title: `${name} (${node.state})`,
                    label: name.length > MAX_LABEL ? `${name.slice(0, MAX_LABEL - 1)}…` : name,
                    current: node.id === current,
                });
            });
        });
        const paths = edges.map(([from, to]) => {
            const a = positions.get(from);
            const b = positions.get(to);
            const x1 = a.x + NODE_WIDTH;
            const y1 = a.y + NODE_HEIGHT / 2;
            const x2 = b.x - 4;
            const y2 = b.y + NODE_HEIGHT / 2;
            const mid = (x1 + x2) / 2;
            return {
                key: `${from}-${to}`,
                d: `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`,
            };
        });
        const rows = Math.max(0, ...columns.map((column) => column.length));
        return {
            boxes,
            paths,
            width: PADDING * 2 + columns.length * (NODE_WIDTH + GAP_X) - GAP_X,
            height: PADDING * 2 + rows * (NODE_HEIGHT + GAP_Y) - GAP_Y,
            nodeWidth: NODE_WIDTH,
            nodeHeight: NODE_HEIGHT,
        };
    }

    async openJob(node) {
        if (node.current) {
            return;
        }
        await this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "gq.job",
            res_id: node.id,
            views: [[false, "form"]],
        });
    }
}

export const gqJobGraph = {
    component: GQJobGraph,
    supportedTypes: ["json"],
};

registry.category("fields").add("gq_job_graph", gqJobGraph);
