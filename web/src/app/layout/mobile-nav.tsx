import { Menu } from 'lucide-react'

import { Button } from '@/components/ui/button'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet'

import { useUi } from '../contexts/ui'
import { Brand } from './brand'
import { NavLinks } from './nav-links'

export function MobileNav() {
  const { mobileNavOpen, setMobileNavOpen } = useUi()
  const close = () => setMobileNavOpen(false)
  return (
    <Sheet open={mobileNavOpen} onOpenChange={setMobileNavOpen}>
      <SheetTrigger asChild>
        <Button variant="ghost" size="icon" className="lg:hidden" aria-label="Open navigation">
          <Menu aria-hidden />
        </Button>
      </SheetTrigger>
      <SheetContent side="left" className="w-72 bg-sidebar p-0 text-sidebar-foreground">
        <SheetHeader className="h-14 flex-row items-center px-4">
          <SheetTitle asChild>
            <div>
              <Brand onNavigate={close} />
            </div>
          </SheetTitle>
          <SheetDescription className="sr-only">Main navigation</SheetDescription>
        </SheetHeader>
        <nav aria-label="Main" className="px-3 py-2">
          <NavLinks onNavigate={close} />
        </nav>
      </SheetContent>
    </Sheet>
  )
}
