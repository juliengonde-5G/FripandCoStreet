import AppShell from "@/components/layout/AppShell";
import Card from "@/components/ui/Card";

export default function AdminPage() {
  return (
    <AppShell>
      <h1 className="text-2xl font-bold text-fc-ink mb-6">Administration</h1>

      <Card>
        <p className="text-fc-ink">
          Administration — paramètres boutique et journal des événements (PR2/PR4).
        </p>
      </Card>
    </AppShell>
  );
}
